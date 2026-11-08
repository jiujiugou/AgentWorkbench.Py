import { handle } from './http'

/** 知识库元数据（REQ-24）。文档继承知识库可见性，不单独设 ACL。 */
export interface KnowledgeBaseDto {
  id: string
  name: string
  visibility: 'public' | 'private' | string
  minLevelRank: number | null
  isDefault: boolean
  createdAt: string
  archivedAt?: string | null
}

export interface CreateKnowledgeBaseInput {
  name: string
  visibility: 'public' | 'private'
  minLevelRank?: number | null
}

export interface UpdateKnowledgeBaseInput {
  name?: string
  visibility?: 'public' | 'private'
  minLevelRank?: number | null
}

export async function listKnowledgeBases(): Promise<KnowledgeBaseDto[]> {
  return handle<KnowledgeBaseDto[]>(await fetch('/api/knowledge-bases'))
}

export async function createKnowledgeBase(
  input: CreateKnowledgeBaseInput,
): Promise<KnowledgeBaseDto> {
  return handle<KnowledgeBaseDto>(
    await fetch('/api/knowledge-bases', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(input),
    }),
  )
}

export async function updateKnowledgeBase(
  id: string,
  input: UpdateKnowledgeBaseInput,
): Promise<KnowledgeBaseDto> {
  return handle<KnowledgeBaseDto>(
    await fetch(`/api/knowledge-bases/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(input),
    }),
  )
}

export function archiveKnowledgeBase(id: string): Promise<void> {
  return fetch(`/api/knowledge-bases/${id}/archive`, { method: 'POST' }).then((r) =>
    handle<void>(r),
  )
}

export function unarchiveKnowledgeBase(id: string): Promise<void> {
  return fetch(`/api/knowledge-bases/${id}/unarchive`, { method: 'POST' }).then((r) =>
    handle<void>(r),
  )
}
