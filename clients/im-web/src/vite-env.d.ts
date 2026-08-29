/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_LOCAL_BEARER_TOKEN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
