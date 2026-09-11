//#defineConfig берётся из vitest: только он знает про раздел test.
//#У vite такого поля в типе нет, и сборка на нём не проходит проверку типов
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

//#порт 5174 выбран, чтобы DataArena и ModelArena можно было держать запущенными одновременно
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    strictPort: true,
    //#прокси избавляет от CORS в разработке и делает пути в коде такими же, как в production
    proxy: {
      "/api": { target: "http://127.0.0.1:8510", changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
});
