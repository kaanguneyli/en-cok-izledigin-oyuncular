import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";


const [inputPath, outputPath] = process.argv.slice(2);
if (!inputPath || !outputPath) {
  throw new Error("Usage: letterboxd_workbook.mjs <input.json> <output.xlsx>");
}

const payload = JSON.parse(await fs.readFile(inputPath, "utf8"));
const workbook = Workbook.create();

const COLORS = {
  ink: "#172126",
  muted: "#5C6B73",
  paper: "#F7F9F8",
  green: "#00A56A",
  greenLight: "#DDF5EA",
  line: "#D8E0DC",
  white: "#FFFFFF",
};

const sheet = workbook.worksheets.add("Oyuncular");
sheet.showGridLines = false;

sheet.mergeCells("A1:G1");
sheet.getRange("A1").values = [[`${payload.username} - oyuncu sıralaması`]];
sheet.getRange("A1:G1").format = {
  fill: COLORS.ink,
  font: { bold: true, color: COLORS.white, size: 16 },
  verticalAlignment: "center",
};
sheet.getRange("A1:G1").format.rowHeight = 34;

sheet.mergeCells("A2:G2");
sheet.getRange("A2").values = [[
  `${payload.summary.totalViews} izlenme; ${payload.summary.uniqueFilms} benzersiz film; ` +
    `${payload.summary.rewatches} tekrar. ` +
    (payload.sortDescription || "Sıralama toplam izlenmeye göredir."),
]];
sheet.getRange("A2:G2").format = {
  fill: COLORS.greenLight,
  font: { color: COLORS.ink, size: 10 },
  verticalAlignment: "center",
};
sheet.getRange("A2:G2").format.rowHeight = 25;

const headers = [[
  "Sıra",
  "Oyuncu",
  "İzlenme",
  "Benzersiz film",
  "Tekrar",
  "Letterboxd",
  "Filmler",
]];
sheet.getRange("A4:G4").values = headers;
sheet.getRange("A4:G4").format = {
  fill: COLORS.green,
  font: { bold: true, color: COLORS.white },
  horizontalAlignment: "left",
  verticalAlignment: "center",
  borders: { preset: "outside", style: "thin", color: COLORS.green },
};
sheet.getRange("A4:G4").format.rowHeight = 24;

if (payload.rows.length) {
  const matrix = payload.rows.map((row) => [
    row.rank,
    row.actor,
    row.appearances,
    row.uniqueFilms,
    row.rewatches,
    row.actorUrl,
    row.films,
  ]);
  const lastRow = matrix.length + 4;
  const dataRange = sheet.getRange(`A5:G${lastRow}`);
  dataRange.values = matrix;
  dataRange.format = {
    font: { color: COLORS.ink, size: 10 },
    verticalAlignment: "center",
    borders: {
      insideHorizontal: { style: "thin", color: COLORS.line },
      bottom: { style: "thin", color: COLORS.line },
    },
  };
  dataRange.format.rowHeight = 24;
  sheet.getRange(`A5:A${lastRow}`).format.horizontalAlignment = "right";
  sheet.getRange(`C5:E${lastRow}`).format.horizontalAlignment = "right";
  sheet.getRange(`A5:A${lastRow}`).format.numberFormat = "#,##0";
  sheet.getRange(`C5:E${lastRow}`).format.numberFormat = "#,##0";

  const table = sheet.tables.add(`A4:G${lastRow}`, true, "ActorsTable");
  table.showFilterButton = true;
} else {
  sheet.mergeCells("A5:G5");
  sheet.getRange("A5").values = [["Kayıt bulunamadı."]];
  sheet.getRange("A5:G5").format = {
    fill: COLORS.paper,
    font: { color: COLORS.muted, italic: true },
    verticalAlignment: "center",
  };
}

sheet.getRange("A:A").format.columnWidth = 9;
sheet.getRange("B:B").format.columnWidth = 28;
sheet.getRange("C:C").format.columnWidth = 14;
sheet.getRange("D:D").format.columnWidth = 18;
sheet.getRange("E:E").format.columnWidth = 12;
sheet.getRange("F:F").format.columnWidth = 48;
sheet.getRange("G:G").format.columnWidth = 110;
sheet.freezePanes.freezeRows(4);

if (payload.errors.length) {
  const sheet = workbook.worksheets.add("Hatalar");
  sheet.showGridLines = false;
  sheet.getRange("A1:C1").values = [["Film", "Film bağlantısı", "Hata"]];
  sheet.getRange("A1:C1").format = {
    fill: "#B42318",
    font: { bold: true, color: COLORS.white },
  };
  sheet.getRange(`A2:C${payload.errors.length + 1}`).values = payload.errors.map((item) => [
    item.film,
    item.filmUrl,
    item.error,
  ]);
  sheet.getRange("A:A").format.columnWidth = 36;
  sheet.getRange("B:B").format.columnWidth = 48;
  sheet.getRange("C:C").format.columnWidth = 70;
  sheet.getRange(`A2:C${payload.errors.length + 1}`).format.wrapText = true;
  sheet.freezePanes.freezeRows(1);
}

await fs.mkdir(path.dirname(outputPath), { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
