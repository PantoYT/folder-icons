// Rasterize SVG strings to PNG with resvg. Jobs arrive as JSON on stdin:
// [{"svg": "<svg ...>", "out": "path.png", "size": 256}, ...]
// Icons carry no text, so system fonts are not loaded (that alone took ~0.2 s per render).
const fs = require("fs");
const { Resvg } = require("@resvg/resvg-js");

const jobs = JSON.parse(fs.readFileSync(0, "utf8"));
for (const job of jobs) {
  const png = new Resvg(job.svg, { fitTo: { mode: "width", value: job.size }, font: { loadSystemFonts: false } }).render().asPng();
  fs.writeFileSync(job.out, png);
}
console.log(`rendered ${jobs.length}`);
