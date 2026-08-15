import { defineConfig } from "vite";
import { copyFile } from "wpsjs/vite_plugins";

export default defineConfig({
  base: "./",
  plugins: [
    copyFile({ src: "main.js", dest: "main.js" }),
    copyFile({ src: "ribbon.js", dest: "ribbon.js" }),
    copyFile({ src: "ribbon.xml", dest: "ribbon.xml" }),
    copyFile({ src: "manifest.xml", dest: "manifest.xml" })
  ],
  server: { host: "127.0.0.1", port: 3889 }
});
