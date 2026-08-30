import { useEffect, useMemo, useState } from "react";
import { api, type Conversation, type Message } from "./api";
import { useUIStore } from "./store";

function newMessageId() {
  return `msg_web_${crypto.randomUUID()}`;
}

function formatTime(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

function conversationLabel(conversation: Conversation) {
  return conversation.name || conversation.id;
}

export function App() {
  const snapshot = useUIStore((state) => state.snapshot);
  const agents = useUIStore((state) => state.agents);
  const conversations = useUIStore((state) => state.conversations);
  const selectedConversationId = useUIStore((state) => state.selectedConversationId);
  const messages = useUIStore((state) => state.messages);
  const mention = useUIStore((state) => state.mention);
  const loading = useUIStore((state) => state.loading);
  const error = useUIStore((state) => state.error);
  const setSnapshot = useUIStore((state) => state.setSnapshot);
  const setAgents = useUIStore((state) => state.setAgents);
  const setConversations = useUIStore((state) => state.setConversations);
  const selectConversation = useUIStore((state) => state.selectConversation);
  const setMessages = useUIStore((state) => state.setMessages);
  const setMention = useUIStore((state) => state.setMention);
  const setLoading = useUIStore((state) => state.setLoading);
  const setError = useUIStore((state) => state.setError);

  const [groupName, setGroupName] = useState("");
  const [includeAgent, setIncludeAgent] = useState(true);
  const [messageText, setMessageText] = useState("");
  const [instruction, setInstruction] = useState("");
  const [memberAction, setMemberAction] = useState("");
  const [conversationFilter, setConversationFilter] = useState("");

  const selectedConversation = useMemo(
    () => conversations.find((conversation) => conversation.id === selectedConversationId),
    [conversations, selectedConversationId],
  );
  const filteredConversations = useMemo(() => {
    const query = conversationFilter.trim().toLocaleLowerCase();
    if (!query) return conversations;
    return conversations.filter((conversation) =>
      `${conversation.name} ${conversation.id}`.toLocaleLowerCase().includes(query),
    );
  }, [conversationFilter, conversations]);

  async function loadMessages(conversationId: string) {
    const page = await api.messages(conversationId);
    setMessages(page.messages);
  }

  async function loadAll() {
    setLoading(true);
    setError("");
    try {
      const [runtime, agentPage, page] = await Promise.all([api.snapshot(), api.agents(), api.conversations()]);
      setSnapshot(runtime);
      setAgents(agentPage.agents);
      setConversations(page.conversations);
      const selectedStillExists = page.conversations.some((conversation) => conversation.id === selectedConversationId);
      const nextId = selectedStillExists ? selectedConversationId : page.conversations[0]?.id || "";
      if (nextId) {
        if (nextId !== selectedConversationId) selectConversation(nextId);
        await loadMessages(nextId);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadAll();
    // Bootstrap intentionally runs once; subsequent actions refresh the affected projection.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function chooseConversation(id: string) {
    selectConversation(id);
    setError("");
    try {
      await loadMessages(id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }

  async function createGroup() {
    const name = groupName.trim();
    if (!name) return;
    setLoading(true);
    setError("");
    try {
      const installedAgent = agents.find((agent) => agent.installationStatus === "active");
      const memberActorIds = includeAgent && installedAgent ? [installedAgent.agentActorId] : [];
      const result = await api.createConversation(name, `web/group/${crypto.randomUUID()}`, memberActorIds);
      setGroupName("");
      const page = await api.conversations();
      setConversations(page.conversations);
      selectConversation(result.conversation.id);
      await loadMessages(result.conversation.id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setLoading(false);
    }
  }

  async function sendMessage() {
    const text = messageText.trim();
    if (!selectedConversationId || !text) return;
    setLoading(true);
    setError("");
    try {
      await api.sendMessage(selectedConversationId, text);
      setMessageText("");
      await loadMessages(selectedConversationId);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setLoading(false);
    }
  }

  async function editMessage(message: Message) {
    const nextText = window.prompt("编辑消息", message.text);
    if (nextText === null || !nextText.trim() || !selectedConversationId) return;
    setError("");
    try {
      await api.editMessage(selectedConversationId, message.id, nextText.trim());
      await loadMessages(selectedConversationId);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }

  async function recallMessage(message: Message) {
    if (!selectedConversationId || !window.confirm("确认撤回这条消息？")) return;
    setError("");
    try {
      await api.recallMessage(selectedConversationId, message.id);
      await loadMessages(selectedConversationId);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }

  async function runMention() {
    const text = instruction.trim();
    const installedAgent = agents.find((agent) => agent.installationStatus === "active");
    if (!text || !selectedConversationId || !installedAgent || !canMention) return;
    setLoading(true);
    setError("");
    try {
      const result = await api.mention(selectedConversationId, newMessageId(), text);
      const page = await api.conversations();
      setConversations(page.conversations);
      selectConversation(result.childConversationId);
      await loadMessages(result.childConversationId);
      setMention(result);
      setInstruction("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setLoading(false);
    }
  }

  async function inviteAgent() {
    if (!selectedConversationId) return;
    const installedAgent = agents.find((agent) => agent.installationStatus === "active");
    if (!installedAgent) return;
    setLoading(true);
    setError("");
    setMemberAction("");
    try {
      const result = await api.addMembers(
        selectedConversationId,
        [installedAgent.agentActorId],
        `web/members/${crypto.randomUUID()}`,
      );
      setMemberAction(result.addedActorIds.length > 0 ? `已邀请 ${installedAgent.name}` : `${installedAgent.name} 已在群中`);
      const page = await api.conversations();
      setConversations(page.conversations);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setLoading(false);
    }
  }

  const isLocal = snapshot?.mode === "zero-network-fake";
  const selectedAgent = agents.find((agent) => agent.installationStatus === "active");
  const canMention = Boolean(
    selectedConversation?.type === "group" &&
    selectedAgent &&
    selectedConversation.memberActorIds.includes(selectedAgent.agentActorId),
  );

  return (
    <div className="min-h-screen bg-ink-bg text-ink">
      <header className="border-b border-white/10 bg-ink-bg/80 backdrop-blur-xl">
        <div className="mx-auto flex max-w-[1500px] items-center justify-between gap-4 px-5 py-4 lg:px-8">
          <div className="flex items-center gap-3">
            <div className="brand-mark" aria-hidden="true"><span /><span /></div>
            <div>
              <div className="text-sm font-semibold tracking-[0.18em] text-white">WANWORK</div>
              <div className="text-xs text-slate-400">v0版 · 人与 Agent 原生协同</div>
            </div>
          </div>
          <div className="flex items-center gap-2 text-xs text-slate-300">
            <span className="status-dot" />
            <span>LOCAL ONLY</span>
            <span className="hidden rounded-full border border-white/10 px-2 py-1 sm:inline">React Web</span>
          </div>
        </div>
      </header>

      <main className="mx-auto grid max-w-[1500px] gap-5 px-5 py-5 lg:grid-cols-[300px_minmax(0,1fr)_340px] lg:px-8">
        <aside className="panel flex min-h-[640px] flex-col p-4">
          <div className="mb-4 flex items-center justify-between">
            <div>
              <div className="text-sm font-semibold text-white">工作空间</div>
              <div className="mt-1 text-xs text-slate-500">产品研发 · 本地沙盒</div>
            </div>
            <span className="rounded-full bg-cyan/10 px-2 py-1 text-[10px] font-semibold text-cyan">{conversations.length}</span>
          </div>
          <div className="mb-4 flex gap-2">
            <input
              value={groupName}
              onChange={(event) => setGroupName(event.target.value)}
              onKeyDown={(event) => { if (event.key === "Enter") void createGroup(); }}
              placeholder="新建群聊"
              className="field min-w-0 flex-1"
              aria-label="新群名称"
            />
            <button className="button-secondary px-3" onClick={() => void createGroup()} disabled={loading}>+</button>
          </div>
          <input
            value={conversationFilter}
            onChange={(event) => setConversationFilter(event.target.value)}
            placeholder="筛选会话名称或 ID"
            className="field mb-4 w-full"
            aria-label="筛选会话"
          />
          <label className="mb-4 flex items-center gap-2 text-xs text-slate-400">
            <input
              type="checkbox"
              checked={includeAgent}
              onChange={(event) => setIncludeAgent(event.target.checked)}
              className="accent-cyan"
            />
            创建时邀请已安装 Agent
          </label>
          <div className="mb-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">会话</div>
          <div className="space-y-1 overflow-y-auto" aria-label="会话列表">
            {filteredConversations.map((conversation) => (
              <button
                key={conversation.id}
                onClick={() => void chooseConversation(conversation.id)}
                className={`conversation-row ${conversation.id === selectedConversationId ? "conversation-row-active" : ""}`}
              >
                <span className="conversation-avatar">{conversationLabel(conversation).slice(0, 1)}</span>
                <span className="min-w-0 flex-1 text-left">
                  <span className="block truncate text-sm font-medium text-slate-200">{conversationLabel(conversation)}</span>
                  <span className="mt-1 block truncate text-[10px] text-slate-500">{conversation.type} · {conversation.providerStatus}</span>
                </span>
              </button>
            ))}
            {filteredConversations.length === 0 && <div className="empty-state px-3 py-5 text-xs">没有匹配的会话</div>}
          </div>
          <div className="mt-auto border-t border-white/10 pt-4 text-xs text-slate-500">
            <div className="flex items-center justify-between"><span>身份</span><span className="text-slate-300">{snapshot?.humanActorId ?? "加载中"}</span></div>
            <div className="mt-2 flex items-center justify-between"><span>Agent Store</span><span className="max-w-[170px] truncate text-right text-cyan">{agents[0]?.name ?? "加载中"}</span></div>
          </div>
        </aside>

        <section className="panel flex min-h-[640px] flex-col overflow-hidden">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/10 px-5 py-4">
            <div>
              <div className="flex items-center gap-2 text-lg font-semibold text-white">
                {selectedConversation ? conversationLabel(selectedConversation) : "选择一个会话"}
                {selectedConversation?.parentConversationId && <span className="rounded-full bg-violet/10 px-2 py-1 text-[10px] text-violet">Agent 子群</span>}
              </div>
              <div className="mt-1 text-xs text-slate-500">{selectedConversation?.id ?? "等待会话加载"}</div>
            </div>
            <div className="text-right text-xs text-slate-400">
              <div>{isLocal ? "zero-network fake" : snapshot ? "explicit model runtime" : "连接中"}</div>
              <div className="mt-1 text-[10px] text-slate-600">
                {snapshot?.agentRuntime ? `${snapshot.agentRuntime.provider} · ${snapshot.agentRuntime.model} · ${snapshot.agentRuntime.status}` : "RongCloud projection · provider outbound off"}
              </div>
            </div>
          </div>

          <div className="flex-1 space-y-3 overflow-y-auto p-5">
            {messages.length === 0 && <div className="empty-state">还没有消息。发送一条普通文本，或在右侧 `@v0版 Agent` 启动一次协作。</div>}
            {messages.map((message) => {
              const mine = message.senderActorId === snapshot?.humanActorId;
              return (
                <div key={message.id} className={`flex ${mine ? "justify-end" : "justify-start"}`}>
                  <div className={`message-bubble ${mine ? "message-mine" : "message-agent"}`}>
                    <div className="mb-1 flex items-center gap-2 text-[10px] text-slate-500">
                      <span>{mine ? "你" : message.senderActorId}</span><span>{message.status}</span><span>{formatTime(message.createdAt)}</span>
                    </div>
                    <div className="whitespace-pre-wrap break-words text-sm leading-6">{message.status === "recalled" ? "（已撤回）" : message.text}</div>
                    {mine && message.status !== "recalled" && <div className="mt-2 flex gap-2"><button className="message-action" onClick={() => void editMessage(message)}>编辑</button><button className="message-action" onClick={() => void recallMessage(message)}>撤回</button></div>}
                  </div>
                </div>
              );
            })}
          </div>

          <div className="border-t border-white/10 p-4">
            <div className="flex gap-2">
              <textarea
                value={messageText}
                onChange={(event) => setMessageText(event.target.value)}
                onKeyDown={(event) => { if ((event.metaKey || event.ctrlKey) && event.key === "Enter") void sendMessage(); }}
                placeholder="在群里发送消息 · ⌘/Ctrl + Enter 发送"
                className="field min-h-[48px] flex-1 resize-none"
                rows={2}
              />
              <button className="button-primary self-end" onClick={() => void sendMessage()} disabled={loading || !selectedConversationId}>发送</button>
            </div>
          </div>
        </section>

        <aside className="space-y-5">
          <section className="panel p-5">
            <div className="mb-1 text-[11px] font-semibold uppercase tracking-[0.16em] text-cyan">Agent Store</div>
            <h2 className="text-xl font-semibold text-white">让 Agent 像普通成员一样协作</h2>
            <p className="mt-2 text-sm leading-6 text-slate-400">@Agent 后创建父群关联的独立工作子群，过程和回复只进入子群；当前体验使用 v0版 fake provider。</p>
            <div className="mt-4 space-y-3">
              {agents.length === 0 && <div className="rounded-xl border border-white/10 bg-white/5 p-3 text-xs text-slate-500">正在读取 Agent Store…</div>}
              {agents.map((agent) => (
                <div key={agent.installationId} className="rounded-xl border border-violet/20 bg-violet/5 p-3 text-xs text-slate-300">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-semibold text-violet">{agent.name}</span>
                    <span className="rounded-full border border-green-300/20 px-2 py-1 text-[10px] text-green-300">{agent.installationStatus}</span>
                  </div>
                  <div className="mt-2 leading-5 text-slate-400">{agent.summary}</div>
                  <div className="mt-2 text-slate-500">release {agent.version} · actor {agent.agentActorId}</div>
                  <div className="mt-2 border-t border-white/10 pt-2 text-slate-400">
                    已授权：<span className="text-slate-200">{agent.grantedCapabilities.join(" · ") || "无"}</span>
                  </div>
                  <div className="mt-1 text-slate-500">
                    数据路线：{agent.dataRoutes.map((route) => `${route.name} → ${route.destinations.join(", ")}`).join("；") || "无"}
                  </div>
                  <div className="mt-1 text-slate-500">Trust Passport：{agent.attestations.length} 项审阅声明 · {agent.passportStatus}</div>
                  <button
                    className="button-secondary mt-3 w-full"
                    onClick={() => void inviteAgent()}
                    disabled={loading || !selectedConversationId || agent.installationStatus !== "active"}
                  >
                    邀请到当前群
                  </button>
                </div>
              ))}
              {memberAction && <div role="status" className="rounded-lg border border-cyan/20 bg-cyan/5 px-3 py-2 text-xs text-cyan">{memberAction}</div>}
            </div>
          </section>

          <section className="panel p-5">
            <div className="mb-1 text-[11px] font-semibold uppercase tracking-[0.16em] text-violet">Mention Router</div>
            <h2 className="text-xl font-semibold text-white">发布协作指令</h2>
            <p className="mt-2 text-sm leading-6 text-slate-400">这会创建一个与父群关联的 Agent 子群，并返回 invocation、工作卡和 Agent 回复。</p>
            <textarea value={instruction} onChange={(event) => setInstruction(event.target.value)} placeholder="例如：调研竞品，给出带证据的 Web 端方案" className="field mt-4 min-h-[92px] w-full resize-none" />
            <button className="button-primary mt-3 w-full" onClick={() => void runMention()} disabled={loading || !instruction.trim() || !canMention}>@v0版 Agent</button>
            {!canMention && <div className="mt-2 text-xs text-amber-300/80">请先选择含 Agent 的普通群；Agent 子群不能再次创建子群。</div>}
            {mention && <div className="mt-4 space-y-3 rounded-xl border border-cyan/20 bg-cyan/5 p-3 text-xs">
              <div className="flex items-center justify-between"><span className="text-slate-400">工作状态</span><span className="text-cyan">{mention.replayed ? "REPLAYED" : "COMMITTED"}</span></div>
              <div className="text-slate-500">子群 <span className="text-slate-300">{mention.childConversationId}</span></div>
              <div className="text-slate-500">Invocation <span className="text-slate-300">{mention.invocationId}</span></div>
              <div className="border-t border-white/10 pt-3 leading-5 text-slate-200">{mention.agentReply.text}</div>
            </div>}
          </section>

          <section className="panel p-5 text-xs text-slate-500">
            <div className="flex items-center justify-between"><span>Runtime</span><span className="text-green-300">{snapshot ? "READY" : "BOOTING"}</span></div>
            <div className="mt-2 flex items-center justify-between"><span>Auth</span><span>{snapshot?.authProvider ?? "—"}</span></div>
            <div className="mt-2 flex items-center justify-between gap-3"><span>Agent runtime</span><span className="truncate text-right">{snapshot?.agentRuntime ? `${snapshot.agentRuntime.mode} · ${snapshot.agentRuntime.model}` : "—"}</span></div>
            <div className="mt-2 flex items-center justify-between"><span>Network calls</span><span className="text-cyan">{snapshot?.networkCalls ?? "—"}</span></div>
            <div className="mt-3 border-t border-white/10 pt-3 leading-5">synthetic 模式不触碰外网；模型模式只访问显式配置的端点。两种模式都不触碰飞书、企微或真实融云；业务错误仍封装在 HTTP 200 envelope 的 `code/data/message` 中。</div>
          </section>
        </aside>
      </main>

      {error && <div role="alert" className="fixed bottom-5 left-1/2 z-30 max-w-[calc(100%-2rem)] -translate-x-1/2 rounded-xl border border-red-300/20 bg-red-950/90 px-4 py-3 text-sm text-red-100 shadow-glow">{error}</div>}
      {loading && <div className="fixed right-5 top-20 rounded-full border border-cyan/20 bg-cyan/10 px-3 py-2 text-xs text-cyan">同步中…</div>}
    </div>
  );
}
