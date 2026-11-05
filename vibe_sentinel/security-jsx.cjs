// Trusted parser input adapter. Never load project modules or TypeScript config.
// Tree-sitter currently rejects bare ampersands in otherwise valid JSX text:
// https://github.com/tree-sitter/tree-sitter-javascript/issues/366
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const ts = require(path.resolve(process.argv[2]));
const names = JSON.parse(fs.readFileSync(0, "utf8"));
if (!Array.isArray(names)) throw new Error("invalid input");
const result = {};
for (const name of names) {
  if (typeof name !== "string" || !/^source-\d+\.(?:[cm]?js|jsx|ts|tsx)$/.test(name)) {
    throw new Error("invalid filename");
  }
  const bytes = fs.readFileSync(name);
  const text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
  const ast = ts.createSourceFile(name, text, ts.ScriptTarget.Latest, true);
  if (ast.parseDiagnostics.length) throw new Error("source parse failed");
  const positions = [];
  const visit = (node) => {
    if (ts.isJsxText(node)) {
      for (let index = node.pos; index < node.end; index++) {
        if (text[index] === "&" &&
            !/^&(?:#\d+|#x[\da-fA-F]+|[A-Za-z][A-Za-z0-9]*);/.test(text.slice(index, node.end))) {
          positions.push(index);
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(ast);
  let character = 0;
  let byte = 0;
  result[name] = positions.map((position) => {
    byte += Buffer.byteLength(text.slice(character, position), "utf8");
    character = position;
    return byte;
  });
}
process.stdout.write(JSON.stringify(result));
