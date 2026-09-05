/* Runs the dashboard's markdown renderer against a report and checks it both
   terminates and produces structure. Invoked by tests/test_web.py when node is
   available. Usage: node render_report.js <app.js> <report.md> */
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const parts = [
  src.match(/const esc = [\s\S]*?\}\[c\]\)\);/)[0],
  src.match(/const BLOCK_START = [^\n]*/)[0],
  src.match(/function md\(src\) \{[\s\S]*?\n\}/)[0],
].join("\n");
const md = new Function(parts + "\nreturn md;")();

const html = md(fs.readFileSync(process.argv[3], "utf8"));
const count = (re) => (html.match(re) || []).length;
console.log(JSON.stringify({
  chars: html.length,
  headings: count(/<h[1-4]>/g),
  tables: count(/<table>/g),
  pre: count(/<pre>/g),
  paragraphs: count(/<p>/g),
  empty_paragraphs: count(/<p><\/p>/g),
}));
