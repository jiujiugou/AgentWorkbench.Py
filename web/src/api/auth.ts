/**
 * 身份 · /api/auth（Cookie 认证）。
 *
 * 契约由后端 `api/routes/identity.py` 定：登录 `POST {email, password}` →
 * `{id, email, role, status, tenantId}`；`GET /me` 未登录是 401。
 *
 * **这里曾经有一个「这个后端没有身份机制」的占位用户**：`/me` 返回 404 时
 * 伪造一个 `local` 身份放行。它在 F1 落地之前是对的（那时确实没有身份端点），
 * 落地之后就成了一个洞——任何一次部署错误（反代把 `/api/auth/*` 吃掉）
 * 都会静默变成「所有人都是本机管理员」，而界面上一切正常。
 * 现在 404 就是一个普通的失败：说不清就说不清，别假装登录着。
 */

import { handle, toApiError } from './http'

export interface AuthUser {
  id: string
  email: string
  role: string
  status: string
  tenantId: string
}

// 登录态缓存：null = 还没查过。登录/登出必须同步改它，否则路由守卫会用旧结果。
let signedIn: boolean | null = null
let pending: Promise<boolean> | null = null

export function cachedSignedIn(): Promise<boolean> {
  if (signedIn !== null) return Promise.resolve(signedIn)
  pending ??= fetchMe().then((user) => {
    signedIn = user !== null
    return signedIn
  })
  return pending
}

/**
 * 忘掉本地登录态（不调后端）。**由 401 的全局处理调用。**
 *
 * 它把状态退回「还没查过」，而不是钉成「已确定未登录」：401 只说明**这一次**
 * 请求的票不行了，而下一次问的时候答案可能已经变了（用户在另一个标签页里重新登录）。
 * 钉死成 `false` 的版本会让那个标签页再也进不来，直到整个页面刷新——
 * 而现象是「明明登录了，这里是空白页」。
 */
export function forgetSession(): void {
  signedIn = null
  pending = null
}

async function fetchMe(): Promise<AuthUser | null> {
  let response: Response
  try {
    response = await fetch('/api/auth/me')
  } catch {
    // 网络层就没通（后端没起）：当作未登录，让守卫把用户送到登录页——
    // 那里会把「连不上」显示出来，比停在一个永远空白的首页强。
    return null
  }

  // 401 是正常路径（还没登录），不是异常：**不走 `handle`**，
  // 否则它会触发全局跳转，而这里正是判断「要不要跳」的地方。
  if (response.status === 401) return null
  if (!response.ok) return null
  return handle<AuthUser>(response)
}

export async function login(email: string, password: string): Promise<AuthUser> {
  const response = await fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  if (!response.ok) {
    // 登录失败也不走 `handle`：它的 401 意思是「口令不对」，
    // 不是「会话过期」，触发全局跳转没有意义（已经在登录页了）。
    throw await toApiError(response)
  }
  const user = await handle<AuthUser>(response)
  signedIn = true
  pending = null
  return user
}

export async function logout(): Promise<void> {
  await handle<void>(await fetch('/api/auth/logout', { method: 'POST' }))
  // 先算注销再改本地状态：网络失败时票还在，界面却显示未登录的话，
  // 下一个请求带着有效的票、界面却让人再登一次。
  signedIn = false
  pending = null
}
