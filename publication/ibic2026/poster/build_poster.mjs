import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, PresentationFile } from "@oai/artifact-tool";

function parseArgs(argv) {
  const values = {};
  for (let i = 0; i < argv.length; i += 2) {
    const key = argv[i];
    const value = argv[i + 1];
    if (!key?.startsWith("--") || value === undefined) {
      throw new Error(`Expected --name value arguments; stopped at ${key ?? "end"}`);
    }
    values[key.slice(2)] = value;
  }
  for (const key of ["starter", "content", "out", "preview", "layout", "manifest"]) {
    if (!values[key]) throw new Error(`Missing required --${key}`);
  }
  return values;
}

async function sha256(file) {
  return crypto.createHash("sha256").update(await fs.readFile(file)).digest("hex");
}

async function pngDimensions(file) {
  const bytes = await fs.readFile(file);
  const signature = "89504e470d0a1a0a";
  if (bytes.length < 24 || bytes.subarray(0, 8).toString("hex") !== signature) {
    throw new Error(`Not a PNG: ${file}`);
  }
  return { width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20) };
}

function assertFinalText(label, value) {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new Error(`Missing poster copy: ${label}`);
  }
  if (/\b(?:pending|provisional|tbd|todo)\b|\[\s*\]/i.test(value)) {
    throw new Error(`Unresolved or provisional poster copy in ${label}: ${value}`);
  }
}

const args = parseArgs(process.argv.slice(2));
const contentPath = path.resolve(args.content);
const contentDir = path.dirname(contentPath);
const content = JSON.parse(await fs.readFile(contentPath, "utf8"));
const textFields = [
  "title",
  "author",
  "methodHeading",
  "methodBody",
  "bestNHCaption",
  "bestNVCaption",
  "ridgeCaption",
  "conclusionHeading",
  "conclusionBody",
  "ridgeContrastCaption",
  "quantitativeBody",
  "hLossCaption",
];
for (const field of textFields) assertFinalText(field, content[field]);
if (!/\d/.test(content.quantitativeBody)) {
  throw new Error("quantitativeBody must contain verifier-derived numeric evidence");
}

const assetRoles = {
  bestNH: "horizontal leakage-controlled Best-N validation",
  bestNV: "vertical leakage-controlled Best-N validation",
  ridgeHV: "exact-paired full-spill H/V ridge comparison",
  ridgeContrast: "selected-H/V exact-paired turn-width contrast",
  hLoss: "horizontal tracking-loss diagnostic",
};
const assets = {};
for (const [key, role] of Object.entries(assetRoles)) {
  const declared = content.assets?.[key];
  if (!declared) throw new Error(`Missing asset path: ${key}`);
  const file = path.resolve(contentDir, declared);
  const dimensions = await pngDimensions(file);
  if (dimensions.width < 500 || dimensions.height < 300) {
    throw new Error(`Undersized ${role} PNG ${dimensions.width}x${dimensions.height}: ${file}`);
  }
  assets[key] = { file, role, dimensions, sha256: await sha256(file) };
}

for (const destination of [args.out, args.preview, args.layout, args.manifest]) {
  await fs.mkdir(path.dirname(path.resolve(destination)), { recursive: true });
}

const starterPptxPath = path.resolve(args.starter);
const deck = await PresentationFile.importPptx(await FileBlob.load(starterPptxPath));
if (deck.slides.items.length !== 1) throw new Error(`Expected one poster slide, found ${deck.slides.items.length}`);
const slide = deck.slides.items[0];

function byName(name) {
  const matches = slide.elements.items.filter((element) => element.name === name);
  if (matches.length !== 1) throw new Error(`Expected one ${name}, found ${matches.length}`);
  return matches[0];
}

function setText(name, value) {
  const element = byName(name);
  element.text.set(value);
  element.text.style = { typeface: "Arial" };
}

async function replaceImage(name, asset) {
  const image = byName(name);
  const position = { ...image.frame };
  const bytes = await fs.readFile(asset.file);
  slide.images.add({
    dataUrl: `data:image/png;base64,${bytes.toString("base64")}`,
    alt: asset.role,
    position,
    fit: "contain",
  });
  image.delete();
}

const title = byName("Text Placeholder 7");
title.text.paragraphs.items[0].setPlainText(content.title);
title.text.paragraphs.items[1].setPlainText(content.author);
title.text.paragraphs.items[2].setPlainText("");
const titleText = title.text.paragraphs.items[0].toPlainText();
const authorText = title.text.paragraphs.items[1].toPlainText();
title.text.getRange(0, titleText.length).fontSize = 120;
title.text.getRange(titleText.length + 1, authorText.length).fontSize = 69.3333;
title.text.getRange(titleText.length + 1, authorText.length).bold = false;
title.text.style = { typeface: "Arial" };

setText("Text Placeholder 17", content.methodHeading);
setText("Text Placeholder 18", content.methodBody);
setText("Text Placeholder 1", content.bestNHCaption);
setText("Text Placeholder 3", content.bestNVCaption);
setText("Text Placeholder 10", content.ridgeCaption);
setText("Text Placeholder 9", content.conclusionHeading);
setText("Text Placeholder 19", content.conclusionBody);
setText("Text Placeholder 4", content.ridgeContrastCaption);
setText("Text Placeholder 20", content.quantitativeBody);
setText("Text Placeholder 11", content.hLossCaption);

await replaceImage("Picture Placeholder 22", assets.bestNH);
await replaceImage("Picture Placeholder 28", assets.bestNV);
await replaceImage("Picture Placeholder 21", assets.ridgeHV);
await replaceImage("Picture Placeholder 15", assets.ridgeContrast);
await replaceImage("Picture Placeholder 30", assets.hLoss);

for (const name of [
  "Picture Placeholder 40",
  "Picture Placeholder 42",
  "Picture Placeholder 45",
  "Picture Placeholder 46",
  "Straight Connector 48",
  "Rectangle 50",
  "Straight Arrow Connector 51",
  "Rectangle 61",
  "Straight Arrow Connector 62",
  "Straight Arrow Connector 64",
  "Straight Arrow Connector 65",
  "Straight Arrow Connector 66",
  "Rectangle 67",
]) {
  byName(name).delete();
}

await fs.writeFile(path.resolve(args.layout), await (await slide.export({ format: "layout" })).text());
await fs.writeFile(
  path.resolve(args.preview),
  new Uint8Array(await (await deck.export({ slide, format: "png", scale: 1 })).arrayBuffer()),
);
await (await PresentationFile.exportPptx(deck)).save(path.resolve(args.out));

const manifest = {
  schema: "tbt-monitor.ibic2026-poster-source/v1",
  starter: { path: starterPptxPath, sha256: await sha256(starterPptxPath) },
  content: { path: contentPath, sha256: await sha256(contentPath) },
  assets,
  outputs: {
    pptx: { path: path.resolve(args.out), sha256: await sha256(path.resolve(args.out)) },
    preview: { path: path.resolve(args.preview), sha256: await sha256(path.resolve(args.preview)) },
    layout: { path: path.resolve(args.layout), sha256: await sha256(path.resolve(args.layout)) },
  },
};
await fs.writeFile(path.resolve(args.manifest), `${JSON.stringify(manifest, null, 2)}\n`);
console.log(JSON.stringify(manifest, null, 2));
