/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 后端地址，默认 http://127.0.0.1:8975 */
  readonly VITE_BACKEND_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
