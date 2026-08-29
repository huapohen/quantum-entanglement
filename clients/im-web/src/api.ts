export type Envelope<T> = {
  code: number;
  data: T | null;
  message: string;
  requestId: string;
};

export type RuntimeSnapshot = {
  mode: string;
  networkCalls: number;
  authProvider: string;
  imProvider: string;
  parentConversationId: string;
  humanActorId: string;
  agentActorId: string;
  agentVersion: string;
};

export type Conversation = {
  id: string;
  type: string;
  status: string;
  name: string;
  workspaceId?: string;
  parentConversationId?: string;
  memberActorIds: string[];
  providerStatus: string;
  createdAt: string;
};

export type ConversationPage = {
  conversations: Conversation[];
  nextCursor?: string;
  hasMore: boolean;
};

export type Message = {
  id: string;
  clientMessageId: string;
  conversationId: string;
  senderActorId: string;
  type: string;
  status: string;
  text: string;
  extInfo?: string;
  providerMessageId?: string;
  providerStatus: string;
  createdAt: string;
};

export type MessagePage = {
  messages: Message[];
  nextCursor?: string;
  hasMore: boolean;
};

export type MentionResult = {
  parentConversationId: string;
  childConversationId: string;
  invocationId: string;
  workCardExtInfo: string;
  agentReply: {
    conversationId: string;
    senderActorId: string;
    text: string;
  };
  replayed: boolean;
  providerStatus: string;
};

export type ConversationResult = { conversation: Conversation; replayed: boolean };
export type MessageResult = { message: Message; replayed: boolean };
export type MutateMessageResult = MessageResult;

const localToken = import.meta.env.VITE_LOCAL_BEARER_TOKEN || "demo.local.signature";

export class APIError extends Error {
  readonly code: number;
  readonly requestId: string;

  constructor(envelope: Envelope<unknown>) {
    super(`${envelope.code}: ${envelope.message}`);
    this.name = "APIError";
    this.code = envelope.code;
    this.requestId = envelope.requestId;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${localToken}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");

  const response = await fetch(path, { ...init, headers });
  let envelope: Envelope<T>;
  try {
    envelope = (await response.json()) as Envelope<T>;
  } catch {
    throw new Error(`服务响应无法解析（HTTP ${response.status}）`);
  }
  if (envelope.code !== 200 || envelope.data === null) throw new APIError(envelope);
  return envelope.data;
}

export const api = {
  snapshot: () => request<RuntimeSnapshot>("/api/v1/demo/im"),
  conversations: () => request<ConversationPage>("/api/v1/demo/im/conversations?limit=50"),
  messages: (conversationId: string) =>
    request<MessagePage>(`/api/v1/demo/im/conversations/${encodeURIComponent(conversationId)}/messages?limit=100`),
  createConversation: (name: string, idempotencyKey: string) =>
    request<ConversationResult>("/api/v1/demo/im/conversations", {
      method: "POST",
      body: JSON.stringify({ type: "group", name, memberActorIds: [], idempotencyKey }),
    }),
  sendMessage: (conversationId: string, text: string) =>
    request<MessageResult>(
      `/api/v1/demo/im/conversations/${encodeURIComponent(conversationId)}/messages`,
      {
        method: "POST",
        body: JSON.stringify({
          clientMessageId: `msg_web_${crypto.randomUUID()}`,
          text,
          // The demo validates canonical JSON bytes (encoding/json map order), so keep keys
          // lexicographically ordered just as the server's metadata codec does.
          extInfo: JSON.stringify({ client: "im-web-v0", messageType: "text" }),
        }),
      },
    ),
  editMessage: (conversationId: string, messageId: string, text: string) =>
    request<MutateMessageResult>(
      `/api/v1/demo/im/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(messageId)}`,
      { method: "PATCH", body: JSON.stringify({ text }) },
    ),
  recallMessage: (conversationId: string, messageId: string) =>
    request<MutateMessageResult>(
      `/api/v1/demo/im/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(messageId)}/recall`,
      { method: "POST", body: "{}" },
    ),
  mention: (messageId: string, instruction: string) =>
    request<MentionResult>("/api/v1/demo/im/mentions", {
      method: "POST",
      body: JSON.stringify({ messageId, instruction }),
    }),
};
