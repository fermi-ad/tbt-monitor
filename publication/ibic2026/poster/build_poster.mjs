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

function portablePath(file) {
  const resolved = path.resolve(file);
  const marker = `${path.sep}publication${path.sep}ibic2026${path.sep}`;
  const markerIndex = resolved.lastIndexOf(marker);
  if (markerIndex >= 0) {
    return resolved.slice(markerIndex + 1).split(path.sep).join("/");
  }
  return `external/${path.basename(resolved)}`;
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
  "subtitle",
  "author",
  "reportNumber",
  "acknowledgment",
  "mapCaption",
  "mapCredit",
  "methodHeading",
  "methodBody",
  "bestNHCaption",
  "bestNVCaption",
  "ridgeHeading",
  "conclusionHeading",
  "conclusionBody",
];
for (const field of textFields) assertFinalText(field, content[field]);
if (!/\d/.test(`${content.bestNHCaption} ${content.bestNVCaption} ${content.conclusionBody}`)) {
  throw new Error("poster copy must contain verifier-derived numeric evidence");
}

const assetRoles = {
  beamlineMap: "Muon Campus beamline layout with the Mu2e Delivery Ring",
  bestNH: "horizontal leakage-controlled Best-N validation",
  bestNV: "vertical leakage-controlled Best-N validation",
  ridgeHV: "exact-paired full-spill H/V ridge comparison",
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

function setText(name, value, options = {}) {
  const element = byName(name);
  element.text.set(value);
  element.text.style = {
    typeface: "Arial",
    ...(options.fontSize ? { fontSize: options.fontSize } : {}),
    ...(options.color ? { color: options.color } : {}),
    ...(options.bold === undefined ? {} : { bold: options.bold }),
    ...(options.alignment ? { alignment: options.alignment } : {}),
    ...(options.verticalAlignment
      ? { verticalAlignment: options.verticalAlignment }
      : {}),
  };
  if (options.frame) element.frame = options.frame;
}

async function replaceImage(name, asset, position) {
  const image = byName(name);
  const destination = position ?? { ...image.frame };
  const bytes = await fs.readFile(asset.file);
  slide.images.add({
    dataUrl: `data:image/png;base64,${bytes.toString("base64")}`,
    alt: asset.role,
    position: destination,
    fit: "contain",
  });
  image.delete();
}

const title = byName("Text Placeholder 7");
title.text.paragraphs.items[0].setPlainText(content.title);
title.text.paragraphs.items[1].setPlainText(content.subtitle);
title.text.paragraphs.items[2].setPlainText(content.author);
const titleText = title.text.paragraphs.items[0].toPlainText();
const subtitleText = title.text.paragraphs.items[1].toPlainText();
const authorText = title.text.paragraphs.items[2].toPlainText();
const subtitleStart = titleText.length + 1;
const authorStart = subtitleStart + subtitleText.length + 1;
title.text.getRange(0, titleText.length).fontSize = 128;
title.text.getRange(0, titleText.length).bold = true;
title.text.getRange(subtitleStart, subtitleText.length).fontSize = 62;
title.text.getRange(subtitleStart, subtitleText.length).bold = false;
title.text.getRange(authorStart, authorText.length).fontSize = 45;
title.text.getRange(authorStart, authorText.length).bold = false;
title.text.style = { typeface: "Arial" };

setText("Text Placeholder 20", content.reportNumber, {
  frame: { left: 2180, top: 32, width: 900, height: 58 },
  fontSize: 27,
  color: "#FFFFFF",
  bold: true,
  alignment: "right",
  verticalAlignment: "middle",
});

setText("Text Placeholder 17", content.methodHeading, {
  frame: { left: 91.33, top: 2325, width: 2990.47, height: 100 },
  fontSize: 60,
  color: "#004C97",
  bold: true,
  verticalAlignment: "middle",
});
setText("Text Placeholder 18", content.methodBody, {
  frame: { left: 91.33, top: 2425, width: 2990.47, height: 110 },
  fontSize: 44,
  color: "#000000",
  bold: false,
});
setText("Text Placeholder 4", content.mapCaption, {
  frame: { left: 91.33, top: 3425, width: 1450, height: 75 },
  fontSize: 30,
  color: "#004C97",
  bold: true,
});
setText("Text Placeholder 11", `${content.acknowledgment}\n${content.mapCredit}`, {
  frame: { left: 91.33, top: 4205, width: 1800, height: 175 },
  fontSize: 23,
  color: "#FFFFFF",
  bold: false,
});
setText("Text Placeholder 1", content.bestNHCaption, {
  frame: { left: 101.56, top: 3300, width: 1425, height: 125 },
  fontSize: 36,
  color: "#004C97",
  bold: true,
});
setText("Text Placeholder 3", content.bestNVCaption, {
  frame: { left: 1656.8, top: 3300, width: 1425, height: 125 },
  fontSize: 36,
  color: "#004C97",
  bold: true,
});
setText("Text Placeholder 10", content.ridgeHeading, {
  frame: { left: 91.33, top: 690, width: 2990.47, height: 110 },
  fontSize: 60,
  color: "#004C97",
  bold: true,
  verticalAlignment: "middle",
});
setText("Text Placeholder 9", content.conclusionHeading, {
  frame: { left: 1646.8, top: 3425, width: 1435, height: 90 },
  fontSize: 54,
  color: "#004C97",
  bold: true,
  verticalAlignment: "middle",
});
setText("Text Placeholder 19", content.conclusionBody, {
  frame: { left: 1646.8, top: 3520, width: 1435, height: 535 },
  fontSize: 42,
  color: "#000000",
  bold: false,
});

await replaceImage("Picture Placeholder 21", assets.beamlineMap, {
  left: 91.33,
  top: 3500,
  width: 1450,
  height: 500,
});
await replaceImage("Picture Placeholder 22", assets.bestNH, {
  left: 91.33,
  top: 2540,
  width: 1435,
  height: 760,
});
await replaceImage("Picture Placeholder 28", assets.bestNV, {
  left: 1646.8,
  top: 2540,
  width: 1435,
  height: 760,
});
await replaceImage("Picture Placeholder 15", assets.ridgeHV, {
  left: 386.5,
  top: 800,
  width: 2400,
  height: 1516,
});
byName("Picture Placeholder 30").delete();

slide.speakerNotes.textFrame.setText(
  [
    "[Sources]",
    "- Accepted paper evidence: publication/ibic2026/results_payload.json (poster evidence gate).",
    "- H and V validation plus ridge-density graphics: publication/ibic2026/reports/publication_figures/poster/.",
    "- Beamline map: Delivery Ring BPM Status 7-16-2026 - DAS .pptx, slide 2; George Deinlein, Fermilab staff; used with full permission confirmed 2026-08-19; extracted asset SHA-256 7422ad58a659d6149139180bd6d35cd1fc9bd05ba94af6314f9fceb069278ce6.",
    `- Fermilab publication requirements: ${content.reportNumber}; ${content.acknowledgment}`,
  ].join("\n"),
);
slide.speakerNotes.setVisible(true);

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
  schema: "tbt-monitor.ibic2026-poster-source/v2",
  starter: { path: portablePath(starterPptxPath), sha256: await sha256(starterPptxPath) },
  content: { path: portablePath(contentPath), sha256: await sha256(contentPath) },
  evidenceGate: {
    path: portablePath(path.resolve(contentDir, "evidence_gate.json")),
    sha256: await sha256(path.resolve(contentDir, "evidence_gate.json")),
  },
  inputManifest: {
    path: portablePath(path.resolve(contentDir, "input_manifest.json")),
    sha256: await sha256(path.resolve(contentDir, "input_manifest.json")),
  },
  assets: Object.fromEntries(
    Object.entries(assets).map(([key, asset]) => [
      key,
      { ...asset, file: portablePath(asset.file) },
    ]),
  ),
  outputs: {
    pptx: { path: portablePath(args.out), sha256: await sha256(path.resolve(args.out)) },
    artifactPreview: { path: portablePath(args.preview), sha256: await sha256(path.resolve(args.preview)) },
    layout: { path: portablePath(args.layout), sha256: await sha256(path.resolve(args.layout)) },
  },
};
await fs.writeFile(path.resolve(args.manifest), `${JSON.stringify(manifest, null, 2)}\n`);
console.log(JSON.stringify(manifest, null, 2));
