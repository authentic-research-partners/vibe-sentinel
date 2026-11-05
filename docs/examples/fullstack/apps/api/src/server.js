import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { apiGreeting } from "./response.js";

const webRoot = new URL("../../web/", import.meta.url);
const staticFiles = new Map([
  ["/", ["index.html", "text/html; charset=utf-8"]],
  ...["main", "request", "view", "format", "format_alt"].map((name) => [
    `/src/${name}.js`, [`src/${name}.js`, "text/javascript; charset=utf-8"],
  ]),
]);

const server = createServer(async (request, response) => {
  if (request.method !== "GET") {
    response.writeHead(405).end("GET only");
    return;
  }
  const pathname = new URL(request.url, "http://127.0.0.1:8765").pathname;
  try {
    if (pathname === "/api/greeting") {
      const upstream = await fetch("http://127.0.0.1:8766/greeting");
      if (!upstream.ok) throw new Error("Python service did not answer");
      const greeting = apiGreeting(await upstream.json());
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify(greeting));
    } else if (staticFiles.has(pathname)) {
      const [file, contentType] = staticFiles.get(pathname);
      const content = await readFile(new URL(file, webRoot));
      response.writeHead(200, { "content-type": contentType }).end(content);
    } else {
      response.writeHead(404).end("Not found");
    }
  } catch {
    response.writeHead(502, { "content-type": "text/plain" });
    response.end("Start the Python service on 127.0.0.1:8766 first.");
  }
});

server.listen(8765, "127.0.0.1", () => {
  console.log("Open http://127.0.0.1:8765 after starting the Python service.");
});
