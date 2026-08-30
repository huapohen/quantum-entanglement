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
  agentRuntime: {
    mode: string;
    provider: string;
    model: string;
    status: string;
  };
};

export type AgentDataRoute = {
  name: string;
  direction: string;
  classification: string;
  destinations: string[];
  retentionDays: number;
};

export type AgentStoreEntry = {
  definitionId: string;
  releaseId: string;
  installationId: string;
  name: string;
  summary: string;
  version: string;
  definitionStatus: string;
  releaseStatus: string;
  passportStatus: string;
  installationStatus: string;
  agentActorId: string;
  isolation: string;
  requestedCapabilities: string[];
  grantedCapabilities: string[];
  dataRoutes: AgentDataRoute[];
  attestations: string[];
  canInstall: boolean;
};

export type AgentStorePage = {
  agents: AgentStoreEntry[];
};

export type AgentStoreInstallResult = {
  agent: AgentStoreEntry;
  replayed: boolean;
};

export type AgentStoreOffboardResult = {
  agent: AgentStoreEntry;
  dataDisposition: AgentStoreDataDisposition;
  removedConversationIds: string[];
  replayed: boolean;
};

/** Data handling policy requested when an Agent is offboarded. */
export type AgentStoreDataDisposition = "retain" | "archive" | "delete";

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
  taskId: string;
  artifactId: string;
  needsYouId: string;
  workCardExtInfo: string;
  agentReply: {
    conversationId: string;
    senderActorId: string;
    text: string;
  };
  replayed: boolean;
  providerStatus: string;
};

export type Task = {
  id: string;
  title: string;
  instruction: string;
  status: string;
  parentConversationId: string;
  childConversationId: string;
  invocationId: string;
  artifactIds: string[];
  needsYouIds: string[];
  createdAt: string;
  updatedAt: string;
};

export type Artifact = {
  id: string;
  taskId: string;
  title: string;
  kind: string;
  content: string;
  status: string;
  digest: string;
  createdAt: string;
  acceptedAt?: string;
  publishedAt?: string;
  publishedMessageId?: string;
};

export type PublishArtifactResult = {
  artifact: Artifact;
  message: Message;
  replayed: boolean;
};

export type NeedsYou = {
  id: string;
  taskId: string;
  artifactId: string;
  kind: string;
  prompt: string;
  status: string;
  createdAt: string;
  resolvedAt?: string;
};

export type TaskPage = { tasks: Task[] };
export type ArtifactPage = { artifacts: Artifact[] };
export type NeedsYouPage = { needsYou: NeedsYou[] };
export type ResolveNeedsYouResult = { needsYou: NeedsYou; task: Task; artifact: Artifact; replayed: boolean };

export type ConversationResult = { conversation: Conversation; replayed: boolean };
export type AddMembersResult = { conversation: Conversation; addedActorIds: string[]; replayed: boolean };
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
  agents: () => request<AgentStorePage>("/api/v1/demo/im/agents"),
  installAgent: (definitionId: string, idempotencyKey: string) =>
    request<AgentStoreInstallResult>(`/api/v1/demo/im/agents/${encodeURIComponent(definitionId)}/install`, {
      method: "POST",
      body: JSON.stringify({ idempotencyKey }),
    }),
  offboardAgent: (definitionId: string, idempotencyKey: string, dataDisposition: AgentStoreDataDisposition = "archive") =>
    request<AgentStoreOffboardResult>(`/api/v1/demo/im/agents/${encodeURIComponent(definitionId)}/offboard`, {
      method: "POST",
      body: JSON.stringify({ idempotencyKey, dataDisposition }),
    }),
  tasks: () => request<TaskPage>("/api/v1/demo/im/tasks"),
  artifacts: () => request<ArtifactPage>("/api/v1/demo/im/artifacts"),
  needsYou: () => request<NeedsYouPage>("/api/v1/demo/im/needs-you"),
  conversations: () => request<ConversationPage>("/api/v1/demo/im/conversations?limit=50"),
  messages: (conversationId: string) =>
    request<MessagePage>(`/api/v1/demo/im/conversations/${encodeURIComponent(conversationId)}/messages?limit=100`),
  searchMessages: (conversationId: string, query: string) =>
    request<MessagePage>(`/api/v1/demo/im/conversations/${encodeURIComponent(conversationId)}/messages/search?q=${encodeURIComponent(query)}`),
  createConversation: (name: string, idempotencyKey: string, memberActorIds: string[] = []) =>
    request<ConversationResult>("/api/v1/demo/im/conversations", {
      method: "POST",
      body: JSON.stringify({ type: "group", name, memberActorIds, idempotencyKey }),
    }),
  addMembers: (conversationId: string, memberActorIds: string[], idempotencyKey: string) =>
    request<AddMembersResult>(
      `/api/v1/demo/im/conversations/${encodeURIComponent(conversationId)}/members`,
      {
        method: "POST",
        body: JSON.stringify({ memberActorIds, idempotencyKey }),
      },
    ),
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
  mention: (conversationId: string, messageId: string, instruction: string) =>
    request<MentionResult>("/api/v1/demo/im/mentions", {
      method: "POST",
      body: JSON.stringify({ conversationId, messageId, instruction }),
    }),
  resolveNeedsYou: (needsYouId: string, decision: "accept" | "reject") =>
    request<ResolveNeedsYouResult>(`/api/v1/demo/im/needs-you/${encodeURIComponent(needsYouId)}/resolve`, {
      method: "POST",
      body: JSON.stringify({ decision }),
    }),
  publishArtifact: (artifactId: string) =>
    request<PublishArtifactResult>(`/api/v1/demo/im/artifacts/${encodeURIComponent(artifactId)}/publish`, {
      method: "POST",
      body: "{}",
    }),
};
