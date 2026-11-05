/**
 * F7 入口分支（D-V4）：知识库拒答后不得自动调用通用对话。
 *
 * 后端已经断言响应停在 refused；这一层断言浏览器这一侧**没有第二条路**——
 * 如果 store 在 refused 之后再调 chatGeneral，用户会先看到拒答，随后又被
 * 一段通用回答盖住，而“资料里没有”就被“模型自己编了一段”替换了。
 */

import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/knowledge', () => ({
  askKnowledge: vi.fn(),
  historyReport: vi.fn(),
  historySources: vi.fn(),
  toReport: vi.fn(),
  toSources: vi.fn(),
}))

vi.mock('@/api/chat', () => ({ chatGeneral: vi.fn() }))

vi.mock('@/api/conversations', () => ({
  conversationsApi: {
    list: vi.fn().mockResolvedValue([]),
    messages: vi.fn(),
    trace: vi.fn().mockRejectedValue(new Error('trace not needed')),
    remove: vi.fn(),
  },
  toRunSummary: vi.fn(),
  toToolSteps: vi.fn(),
}))

vi.mock('@/api/settings', () => ({
  settingsApi: { listProviders: vi.fn().mockResolvedValue([]) },
}))

import { askKnowledge } from '@/api/knowledge'
import { chatGeneral } from '@/api/chat'
import { useAgentStore } from '@/stores/agent'

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('知识库拒答', () => {
  it('test_refusal_does_not_fall_back_to_general_chat', async () => {
    vi.mocked(askKnowledge).mockResolvedValue({
      conversationId: 'c1',
      runId: 'r1',
      status: 'refused',
      reason: 'knowledge_insufficient',
      verdict: 'insufficient',
      answer: '资料不足以回答该问题。',
      report: null,
      sources: [],
    })
    const store = useAgentStore()

    await store.ask('资料里没有的问题')

    expect(chatGeneral).not.toHaveBeenCalled()
    const reply = store.messages.find((message) => message.role === 'assistant')
    expect(reply).toBeDefined()
    expect(store.refusals[String(reply?.id)].text).toBe('资料不足以回答该问题。')
  })
})
