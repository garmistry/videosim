import { defineConfig } from "vite";

export default defineConfig({
  build: {
    emptyOutDir: true,
    outDir: "videosim/static",
    rollupOptions: {
      input: "frontend/src/main.jsx",
      output: {
        entryFileNames: "app.js",
        assetFileNames: "app.css"
      }
    }
  }
});
