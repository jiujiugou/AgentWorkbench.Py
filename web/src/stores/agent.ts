import { defineStore } from 'pinia'
import { computed, ref, shallowRef } from 'vue'
import * as signalR from '@microsoft/signalr'
import { ElMessage } from 'element-plus'
import type { CitationReport, RefusalInfo, RetrievalSource, RunSummary, ToolStepEvent } from '@/types'
import { askKnowledge, historyReport, historySources, toReport, toSources } from '@/api/knowledge'
import { chatGeneral } from '@/api/chat'
import { cancelRun } from '@/api/runs'
import {
  conversationsApi,
  toRunSummary,
  toToolSteps,
  type Conversation,
  type ConversationKind,
} from '@/api/conversations'
import { settingsApi, type Provider } from '@/api/settings'

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  pending?: boolean
  /**
   * 历史消息带回来的那次运行终态。只读：它来自 `agent_runs`，不在前端重算。
   * `running` + 空正文 = 那一轮没跑完（进程可能已中断），与“跑完但空”分开显示。
   */
  runStatus?: string | null
  /** 这条消息所属会话的答案合同。general 时界面必须标“未使用企业资料”。 */
  mode?: ConversationKind
}

export type ConnectionStatus = 'disconnected' | 'connecting' | 'connected'

const CONVERSATION_KEY = 'aw.conversationId'
const DEFAULT_TITLE = '新会话'

/**
 * Agent 全局状态（单例 store）：
 * - SignalR 连接与流式消息
 * - 多会话管理（新建懒创建 / 切换 / 删除；服务端持久化）
 * - 模型供应商与当前模型（对话页与设置页共享）
 */
export const useAgentStore = defineStore('agent', () => {
  const connection = shallowRef<signalR.HubConnection | null>(null)
  const status = ref<ConnectionStatus>('disconnected')
  const messages = ref<ChatMessage[]>([])
  const generating = ref(false)
  /** 当前在飞的作答：停止时先收口 run，再掐断这条 fetch，避免迟到响应盖住“已停止”。 */
  let activeController: AbortController | null = null
  let activeReplyId: string | null = null
  const conversationId = ref<string | null>(null)
  const conversations = ref<Conversation[]>([])
  const sources = ref<Record<string, RetrievalSource[]>>({})
  /** 拒答：文案与归类码由服务端给，前端只负责把它渲染成"系统的结论"而不是模型的话。 */
  const refusals = ref<Record<string, RefusalInfo>>({})
  /** 每条助手消息的引用体检报告（角标 / 底纹 / cleanedAnswer 的唯一来源），Done 前后到都行。 */
  const citations = ref<Record<string, CitationReport>>({})
  /** 每条助手消息的工具调用时间线（执行中逐步追加）。 */
  const toolSteps = ref<Record<string, ToolStepEvent[]>>({})
  /** 每条助手消息的 run 摘要（迭代数/耗时/结束原因），Done 时才到。 */
  const runs = ref<Record<string, RunSummary>>({})
  /** 中止时的终态（cancelled / timeout / failed）；取消路径没有 run 摘要，靠它标状态。 */
  const stops = ref<Record<string, string>>({})
  const loadingConversation = ref(false)
  /**
   * 新建对话弹窗带过来的第一句。**只在这里过一手，不在弹窗里直接发**：
   * 发送要落在 ChatView（它管滚动、自动增高、generating 态），弹窗只管收集意图。
   * ChatView 读走之后必须清空——不清的话，从别的页面回到对话页会再发一次。
   */
  const pendingQuestion = ref('')

  const providers = ref<Provider[]>([])
  const selectedModelId = ref<number | null>(null)

  // ---------- 会话标题 ----------

  /**
   * 标题**只从库里来**。
   *
   * 以前这里是 `derivedTitles[id] || c.title`：前端拿首条消息自己截前 18 字，
   * 再盖在服务端标题上。两份恰好一样时看不出问题，一旦规则改动（比如改成 20 字），
   * 两台设备会因为各自的会话加载顺序不同而显示不同的名字——而库里那份是对的。
   * 标题现在由 `derive_title` 在服务端写定一次（C-V9），前端不再派生。
   */
  function titleFor(c: Conversation): string {
    return c.title || DEFAULT_TITLE
  }

  // ---------- 会话列表 ----------

  async function refreshConversations(): Promise<void> {
    try {
      const list = await conversationsApi.list()
      conversations.value = list
    } catch {
      /* 侧栏列表加载失败不打扰主流程 */
    }
  }

  async function selectConversation(id: string): Promise<void> {
    if (generating.value) {
      ElMessage.warning('生成中，请先停止或等待完成')
      return
    }
    loadingConversation.value = true
    try {
      const thread = await conversationsApi.messages(id)
      const history = thread.messages
      conversationId.value = id
      messages.value = history.map((m) => ({
        id: String(m.id),
        role: m.role,
        content: m.content,
        runStatus: m.runStatus,
        mode: thread.kind,
      }))

      // 逐条恢复"答案 + 依据 + 过程"三块，而不是只恢复正文。
      //
      // 以前的写法是读完正文就把 sources / citations / toolSteps 全清空：
      // 界面上历史回答变成"一段没有出处的文字"，用户会以为引用没存下来。
      // 现在这三块都从库里读回来——引用读的是**当时的快照**，不回表查 chunks。
      const nextSources: Record<string, RetrievalSource[]> = {}
      const nextCitations: Record<string, CitationReport> = {}
      const nextRefusals: Record<string, RefusalInfo> = {}
      for (const m of history) {
        if (m.role !== 'assistant') continue
        const key = String(m.id)
        const cards = historySources(m)
        if (cards.length) nextSources[key] = cards
        const report = historyReport(m)
        if (report) nextCitations[key] = report

        // 拒答要在重开之后**还是拒答**（C-V13）。
        //
        // 判据用的是服务端带回来的 `runStatus`——那次运行的终态，写一次就不变。
        // 刻意不做的两件事：拿正文和那句拒答文案比（改文案那天全部历史会一起
        // 变成普通回答，而没有任何东西会红）；拿当前阈值重新判一遍（等于让
        // "当时"随参数改口）。正文直接用库里那份，一字不改。
        if (m.runStatus === 'refused') {
          nextRefusals[key] = { text: m.content, code: m.runReason }
        }
      }

      sources.value = nextSources
      citations.value = nextCitations
      refusals.value = nextRefusals
      // 过程要按 run 一条条取。先清空：切会话时旧消息的 key 还在，不清就会
      // 让新会话的某条消息意外继承另一条的轨迹（key 都是数字 id，很容易撞）。
      toolSteps.value = {}
      runs.value = {}
      stops.value = {}

      persistConversationId(id)

      // 轨迹逐条拉，**不阻塞正文与引用的显示**：它们已经在上面设好了，
      // 过程那一块晚几百毫秒到不影响阅读。
      await Promise.all(
        history
          .filter((m) => m.role === 'assistant' && m.runId)
          .map((m) => loadTrace(String(m.id), String(m.runId))),
      )
    } catch {
      ElMessage.error('加载会话失败')
    } finally {
      loadingConversation.value = false
    }
  }

  function newChat(): void {
    if (generating.value) {
      ElMessage.warning('生成中，请先停止或等待完成')
      return
    }
    conversationId.value = null
    messages.value = []
    sources.value = {}
    refusals.value = {}
    citations.value = {}
    toolSteps.value = {}
    runs.value = {}
    stops.value = {}
    persistConversationId(null)
  }

  async function deleteConversation(id: string): Promise<void> {
    if (generating.value && id === conversationId.value) {
      ElMessage.warning('生成中，请先停止或等待完成')
      return
    }
    try {
      await conversationsApi.remove(id)
      if (conversationId.value === id) newChat()
      await refreshConversations()
      ElMessage.success('已删除会话')
    } catch {
      ElMessage.error('删除失败')
    }
  }

  /** 清空 = 删除当前会话并开新的（保留旧语义）。 */
  async function clearCurrent(): Promise<void> {
    if (conversationId.value) {
      const id = conversationId.value
      conversationId.value = null
      messages.value = []
      sources.value = {}
      refusals.value = {}
      citations.value = {}
      toolSteps.value = {}
      runs.value = {}
      stops.value = {}
      persistConversationId(null)
      try {
        await conversationsApi.remove(id)
      } catch {
        /* best effort */
      }
      await refreshConversations()
    } else {
      newChat()
    }
  }

  function persistConversationId(id: string | null): void {
    try {
      if (id) localStorage.setItem(CONVERSATION_KEY, id)
      else localStorage.removeItem(CONVERSATION_KEY)
    } catch {
      /* ignore storage errors */
    }
  }

  // ---------- 模型 ----------

  async function loadProviders(): Promise<Provider[]> {
    try {
      const list = await settingsApi.listProviders()
      providers.value = list
      resolveSelection(list)
      return list
    } catch {
      providers.value = []
      selectedModelId.value = null
      return []
    }
  }

  /** 默认选中：当前启用供应商的默认模型（无则取首个），否则清空无效选择。 */
  function resolveSelection(list: Provider[]): void {
    const active = list.find((p) => p.isActive) ?? null
    const activeModels = active?.models ?? []
    const def = activeModels.find((m) => m.isDefault) ?? activeModels[0]
    if (def) {
      selectedModelId.value = def.id
    } else if (!list.some((p) => p.models.some((m) => m.id === selectedModelId.value))) {
      selectedModelId.value = null
    }
  }

  async function switchModel(id: number): Promise<void> {
    const option = modelOptions.value.find((o) => o.modelId === id)
    if (!option) return
    const provider = providers.value.find((p) => p.id === option.providerId)
    if (!provider) return
    try {
      if (!provider.isActive) await settingsApi.activateProvider(provider.id)
      await settingsApi.setDefaultModel(provider.id, id)
      ElMessage.success(`已切换模型：${option.label}`)
    } catch (e) {
      ElMessage.error('切换失败：' + (e as Error).message)
    } finally {
      await loadProviders()
    }
  }

  /** 所有供应商下的模型拍平，供下拉分组切换（模型 id 全局唯一）。 */
  const modelOptions = computed(() =>
    providers.value.flatMap((p) =>
      p.models.map((m) => ({
        modelId: m.id,
        providerId: p.id,
        providerName: p.name,
        label: m.displayName?.trim() || m.model,
      })),
    ),
  )

  const currentModel = computed(() => {
    const o = modelOptions.value.find((x) => x.modelId === selectedModelId.value) ?? null
    return o ? { providerName: o.providerName, label: o.label } : null
  })

  // ---------- SignalR ----------

  function findOrCreate(messageId: string): ChatMessage {
    let msg = messages.value.find((m) => m.id === messageId)
    if (!msg) {
      msg = { id: messageId, role: 'assistant', content: '', pending: true }
      messages.value.push(msg)
    }
    return msg
  }

  /**
   * 建 SignalR 连接。**当前后端是 Python，没有 hub**——问答走 `api/knowledge.ts`
   * 的一次请求-响应，这里不再被调用。
   *
   * 保留它是因为两版后端并存：C# 那版靠 `/hubs/agent` 逐字推流，
   * 换成 C# 后端时只需让 ChatView 重新调一次 `connect()`。
   */
  async function connect(): Promise<void> {
    if (connection.value && status.value === 'connected') return
    if (connection.value) {
      try {
        await connection.value.stop()
      } catch {
        /* ignore */
      }
      connection.value = null
    }
    status.value = 'connecting'

    const conn = new signalR.HubConnectionBuilder().withUrl('/hubs/agent').withAutomaticReconnect().build()

    conn.on('ReceiveConversation', (p: { conversationId: string }) => {
      if (p?.conversationId) {
        conversationId.value = p.conversationId
        persistConversationId(p.conversationId)
        void refreshConversations()
      }
    })

    conn.on('ReceiveDelta', (p: { messageId: string; delta: string }) => {
      findOrCreate(p.messageId).content += p.delta
    })

    conn.on('ReceiveSources', (p: { messageId: string; sources: RetrievalSource[] }) => {
      findOrCreate(p.messageId)
      sources.value[p.messageId] = p.sources
    })

    // 引用体检：句级统计 + 逐条校验结论 + 剥掉越界标注后的正文。
    // 比正文流晚到（run 结束才算）——角标会在答案渲染完之后补挂，闪一下属预期。
    conn.on('ReceiveCitations', (p: { messageId: string } & CitationReport) => {
      findOrCreate(p.messageId)
      citations.value[p.messageId] = {
        sentenceCount: p.sentenceCount,
        citedSentenceCount: p.citedSentenceCount,
        uncitedSentenceCount: p.uncitedSentenceCount,
        outOfRangeCount: p.outOfRangeCount,
        unsupportedCount: p.unsupportedCount,
        uncitedRatio: p.uncitedRatio,
        supportVerdict: p.supportVerdict,
        cleanedAnswer: p.cleanedAnswer,
        citations: p.citations,
      }
    })

    // 拒答：服务端在工具返回后判定"资料不足"，直接终止生成并给出固定文案。
    // 这里要把正文替换掉——模型可能先吐过一两句（比如"我先查一下"），拒答之后那些话不该留在界面上；
    // 同时清空来源：判定不足时那五块仍是"检索命中的内容"，但用它当出处等于给假担保。
    conn.on('ReceiveRefusal', (p: { messageId: string; text: string; code?: string | null }) => {
      const message = findOrCreate(p.messageId)
      message.content = p.text
      refusals.value[p.messageId] = { text: p.text, code: p.code ?? null }
      delete sources.value[p.messageId]
      delete citations.value[p.messageId]
    })

    // 执行过程：每次工具调用完成推一条，按到达顺序拼成时间线（不依赖 Done，执行中就能看见）。
    conn.on('ReceiveTool', (p: { messageId: string } & ToolStepEvent) => {
      findOrCreate(p.messageId)
      const list = toolSteps.value[p.messageId] ?? (toolSteps.value[p.messageId] = [])
      list.push({
        iteration: p.iteration,
        name: p.name,
        arguments: p.arguments,
        resultPreview: p.resultPreview,
        error: p.error,
      })
    })

    conn.on('ReceiveDone', (p: { messageId: string; conversationId?: string; run?: RunSummary | null }) => {
      findOrCreate(p.messageId).pending = false
      if (p.run) runs.value[p.messageId] = p.run
      if (p?.conversationId) setConversationFromDone(p.conversationId)
      generating.value = false
      void refreshConversations()
    })

    // 中止（用户停止 / 超时 / 出错）：轨迹条要能标出终态，否则「取消」看起来像「答完了」。
    conn.on('ReceiveStopped', (p: { messageId: string; status?: string | null; error?: string | null }) => {
      findOrCreate(p.messageId)
      if (p.status) stops.value[p.messageId] = p.status
    })

    conn.on('Error', (p: { message: string }) => {
      generating.value = false
      const pending = [...messages.value].reverse().find((m) => m.pending)
      if (pending) {
        pending.pending = false
        pending.content += (pending.content ? '\n\n' : '') + '⚠️ ' + p.message
      } else {
        messages.value.push({ id: crypto.randomUUID(), role: 'assistant', content: '⚠️ ' + p.message })
      }
    })

    conn.onreconnecting(() => {
      status.value = 'connecting'
    })
    conn.onreconnected(() => {
      status.value = 'connected'
    })
    conn.onclose(() => {
      status.value = 'disconnected'
    })

    await conn.start()
    connection.value = conn
    status.value = 'connected'
  }

  function setConversationFromDone(id: string): void {
    if (conversationId.value !== id) {
      conversationId.value = id
      persistConversationId(id)
    }
  }

  /**
   * 把一次作答的结果写进 store。
   *
   * 与 SignalR 那套 on('ReceiveXxx') 是同一件事的两个来源：那边来一条推一条，
   * 这边一次到齐。**写入的落点必须与那边完全一致**——否则同一条消息在两种
   * 后端下会渲染成两种样子，而这种差异只有拿两版并排看才会发现。
   */
  /**
   * 取一次运行的轨迹，填进"过程"那一块。
   *
   * 用展开赋值而不是就地改对象：`toolSteps` 是 `ref`，Vue 只追踪通过 `.value`
   * 赋进去的那个代理。在异步回调里改 `nextSteps[key]` 改的是**原始对象**，
   * 界面不会更新——而它不会报错，只是永远显示"没有过程"。
   *
   * 失败静默：答案与引用已经在手，缺的只是过程。为它弹一个错，
   * 会让人以为这次回答本身出了问题。
   */
  async function loadTrace(messageId: string, runId: string): Promise<void> {
    try {
      const trace = await conversationsApi.trace(runId)
      toolSteps.value = { ...toolSteps.value, [messageId]: toToolSteps(trace) }
      runs.value = { ...runs.value, [messageId]: toRunSummary(trace) }
    } catch {
      /* 过程取不到不影响答案 */
    }
  }

  function applyResponse(messageId: string, response: Awaited<ReturnType<typeof askKnowledge>>): void {
    const message = findOrCreate(messageId)
    message.pending = false
    message.mode = 'knowledge'
    message.content = response.answer

    if (response.status === 'refused') {
      // 拒答：文案由服务端写死。同时清掉来源与报告——判定不足时那几块仍是
      // "检索命中的内容"，拿它当出处等于给假担保（与 ReceiveRefusal 同一条理由）。
      refusals.value[messageId] = { text: response.answer, code: response.reason }
      delete sources.value[messageId]
      delete citations.value[messageId]
    } else {
      sources.value[messageId] = toSources(response)
      const report = toReport(response)
      if (report) citations.value[messageId] = report
    }

    // 兜住落在哪条会话。新建时服务端生成 id，不记下来的话下一次追问会再开一条。
    conversationId.value = response.conversationId
    persistConversationId(response.conversationId)

    // 过程单独拉一次：一次请求-响应的协议里没有"边跑边推"，
    // 但轨迹已经在库里了（写入侧先落 running 行），所以这里取得到。
    void loadTrace(messageId, response.runId)
  }

  function applyGeneralResponse(
    messageId: string,
    response: Awaited<ReturnType<typeof chatGeneral>>,
  ): void {
    const message = findOrCreate(messageId)
    message.pending = false
    message.mode = 'general'
    message.content = response.answer

    // 通用回答没有来源、引用或拒答卡。主动清掉，避免切入口后继承上一条
    // 知识库回答的卡片——清理是“这条回答没有依据”的结构表达，不靠 key
    // 永远不重复。
    delete sources.value[messageId]
    delete citations.value[messageId]
    delete refusals.value[messageId]

    conversationId.value = response.conversationId
    persistConversationId(response.conversationId)
    void loadTrace(messageId, response.runId)
  }

  async function dispatch(method: 'Send' | 'Ask', text: string): Promise<void> {
    const content = text.trim()
    if (!content || generating.value) return

    messages.value.push({ id: crypto.randomUUID(), role: 'user', content })
    const replyId = crypto.randomUUID()
    messages.value.push({ id: replyId, role: 'assistant', content: '', pending: true })
    generating.value = true

    const controller = new AbortController()
    activeController = controller
    activeReplyId = replyId

    try {
      if (method === 'Ask') {
        // 知识库入口：有检索、引用与拒答。
        const response = await askKnowledge(content, conversationId.value, controller.signal)
        applyResponse(replyId, response)
      } else {
        // 通用入口：不检索、不引用，响应明确标识 mode=general。
        const response = await chatGeneral(content, conversationId.value, controller.signal)
        applyGeneralResponse(replyId, response)
      }
      // 新建的会话要出现在侧栏。不刷新的话它只在下次进页面时才出现，
      // 用户会以为"刚才那次对话没保存"（而实际上已经落库了）。
      await refreshConversations()
    } catch (e) {
      const message = findOrCreate(replyId)
      message.pending = false
      if (controller.signal.aborted) {
        // 停止路径先收口 run，再 abort 这条 fetch；AbortError 不是“回答失败”，
        // 不能渲染成警告文案。
        message.content = ''
        message.runStatus = 'cancelled'
        stops.value[replyId] = 'cancelled'
        return
      }
      // 记录写不进去（audit_unavailable）走到这里：这次回答不算成功，
      // 所以界面上不该出现半个答案，只出现错误。
      message.content = '⚠️ ' + (e as Error).message
    } finally {
      if (activeController === controller) activeController = null
      if (activeReplyId === replyId) activeReplyId = null
      generating.value = false
    }
  }

  const send = (text: string) => dispatch('Send', text)
  const ask = (text: string) => dispatch('Ask', text)

  async function stop(): Promise<void> {
    if (!generating.value) return

    let result: Awaited<ReturnType<typeof cancelRun>>
    try {
      // Python 后端没有 hub：停止是一条普通 HTTP 请求，把 run 收口成 cancelled。
      // 新会话首问时 conversationId 还是 null，服务端按“当前用户最新的 running”
      // 找到它，并在响应里把新会话 id 交回来。
      result = await cancelRun({ conversationId: conversationId.value })
    } catch (e) {
      ElMessage.error(`停止失败：${(e as Error).message}`)
      return
    }

    // 停止来晚了：run 已经是终态，或模型刚好返回、assistant 步已完整。
    // 这时不 abort——让正常响应落地，不要用“已停止”覆盖一次已经完成的回答。
    if (!result.cancelled) return

    if (result.conversationId) {
      conversationId.value = result.conversationId
      persistConversationId(result.conversationId)
    }

    const controller = activeController
    const replyId = activeReplyId
    controller?.abort()

    if (replyId) {
      const message = findOrCreate(replyId)
      message.pending = false
      message.content = ''
      message.runStatus = 'cancelled'
      stops.value[replyId] = 'cancelled'
      // 已发出未返回的那一步要能在“执行过程”里看到；runId 由停止响应带回。
      if (result.runId) void loadTrace(replyId, result.runId)
    }

    generating.value = false
    void refreshConversations()
  }

  // ---------- 初始化 ----------

  /** 刷新/重新打开页面后恢复：会话列表 + 模型列表 + 上次的会话。 */
  async function restore(): Promise<void> {
    await Promise.all([refreshConversations(), loadProviders()])
    let saved: string | null = null
    try {
      saved = localStorage.getItem(CONVERSATION_KEY)
    } catch {
      saved = null
    }
    if (saved && conversations.value.some((c) => c.id === saved)) {
      await selectConversation(saved)
    } else {
      persistConversationId(null)
      newChat()
    }
  }

  return {
    // 连接与消息
    connection,
    status,
    messages,
    generating,
    sources,
    refusals,
    citations,
    toolSteps,
    runs,
    stops,
    conversationId,
    connect,
    send,
    ask,
    stop,
    // 会话
    conversations,
    loadingConversation,
    pendingQuestion,
    titleFor,
    refreshConversations,
    selectConversation,
    newChat,
    deleteConversation,
    clearCurrent,
    restore,
    // 模型
    providers,
    modelOptions,
    selectedModelId,
    currentModel,
    loadProviders,
    switchModel,
  }
})
