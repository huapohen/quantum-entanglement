import { create } from "zustand";
import type { AgentStoreEntry, Conversation, Message, MentionResult, RuntimeSnapshot } from "./api";

type UIState = {
  snapshot: RuntimeSnapshot | null;
  agents: AgentStoreEntry[];
  conversations: Conversation[];
  selectedConversationId: string;
  messages: Message[];
  mention: MentionResult | null;
  loading: boolean;
  error: string;
  setSnapshot: (snapshot: RuntimeSnapshot) => void;
  setAgents: (agents: AgentStoreEntry[]) => void;
  setConversations: (conversations: Conversation[]) => void;
  selectConversation: (conversationId: string) => void;
  setMessages: (messages: Message[]) => void;
  setMention: (mention: MentionResult | null) => void;
  setLoading: (loading: boolean) => void;
  setError: (error: string) => void;
};

export const useUIStore = create<UIState>((set) => ({
  snapshot: null,
  agents: [],
  conversations: [],
  selectedConversationId: "",
  messages: [],
  mention: null,
  loading: false,
  error: "",
  setSnapshot: (snapshot) => set({ snapshot }),
  setAgents: (agents) => set({ agents }),
  setConversations: (conversations) => set({ conversations }),
  selectConversation: (selectedConversationId) => set({ selectedConversationId, messages: [], mention: null }),
  setMessages: (messages) => set({ messages }),
  setMention: (mention) => set({ mention }),
  setLoading: (loading) => set({ loading }),
  setError: (error) => set({ error }),
}));
