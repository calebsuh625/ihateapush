/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the API. Leave unset in local dev: "/api" is proxied by Vite (see vite.config.ts). */
  readonly VITE_API_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
