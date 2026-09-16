import { describe, expect, it } from "vitest";
import { LOG_LINES } from "../../test/fixtures";
import {
  DEFAULT_LOG_SOURCE, EMPTY_BY_CONFIG, LOG_SOURCES, beforeLoadMessage, downloadNote, emptyMessage, filterLines,
  highlightParts, logSourceLabel, parseLogLine, shortLogger, shortTime, showingLabel, truncationNote,
} from "./logSources";

describe("LOG_SOURCES", () => {
  it("is the four sources, in the order the control offers them, with the app one first", () => {
    expect(LOG_SOURCES.map((source) => [source.value, source.label])).toEqual([
      ["app", "app"], ["xray-stderr", "xray output"], ["xray-error", "xray error file"], ["xray-access", "xray access file"],
    ]);
    expect(DEFAULT_LOG_SOURCE).toBe("app");
    expect(logSourceLabel("xray-stderr")).toBe("xray output");
    expect(logSourceLabel("bogus")).toBe("bogus");
  });

  it("marks exactly the two files xray is not configured to write", () => {
    expect(LOG_SOURCES.filter((source) => source.emptyByConfig).map((source) => source.value)).toEqual(["xray-error", "xray-access"]);
  });

  it("the origins are display-only constants, never an absolute path the gateway disclosed", () => {
    expect(LOG_SOURCES.map((source) => source.origin)).toEqual([
      "data/app.log", "in memory · redacted, last 8 KB", "data/xray-error.log", "data/xray-access.log",
    ]);
    expect(LOG_SOURCES.every((source) => !source.origin.startsWith("/"))).toBe(true);
  });
});

describe("filterLines", () => {
  it("is a case-insensitive substring over the lines already loaded", () => {
    expect(filterLines(LOG_LINES, "ERROR")).toHaveLength(2);
    expect(filterLines(LOG_LINES, "error")).toHaveLength(2);
    expect(filterLines(LOG_LINES, "sigkill")).toHaveLength(1);
    expect(filterLines(LOG_LINES, "")).toEqual(LOG_LINES);
    expect(filterLines(LOG_LINES, "   ")).toEqual(LOG_LINES);
    expect(filterLines(LOG_LINES, "nothing here")).toEqual([]);
  });

  it("returns a copy, so the pane never mutates what was loaded", () => {
    expect(filterLines(LOG_LINES, "")).not.toBe(LOG_LINES);
  });
});

describe("highlightParts", () => {
  it("splits every occurrence out, keeping the line's own casing", () => {
    expect(highlightParts("xray reloaded, Xray again", "xray")).toEqual([
      { text: "xray", match: true },
      { text: " reloaded, ", match: false },
      { text: "Xray", match: true },
      { text: " again", match: false },
    ]);
  });

  it("with no filter the whole line is one unmatched part", () => {
    expect(highlightParts("a line", "")).toEqual([{ text: "a line", match: false }]);
    expect(highlightParts("a line", "  ")).toEqual([{ text: "a line", match: false }]);
  });

  it("handles a match at the very start and at the very end, and joins back to the original", () => {
    for (const query of ["a", "line", "a line", "x"]) {
      expect(highlightParts("a line", query).map((part) => part.text).join("")).toBe("a line");
    }
    expect(highlightParts("aaa", "a")).toHaveLength(3);
  });
});

describe("parseLogLine", () => {
  it("reads the app formatter's four fields", () => {
    expect(parseLogLine(LOG_LINES[0]!)).toEqual({
      time: "2023-11-14 22:10:58,003",
      level: "INFO",
      logger: "pi_gw_panel.xray_supervisor.supervisor",
      message: "starting xray (pid 2417)",
    });
    expect(parseLogLine(LOG_LINES[2]!)?.level).toBe("ERROR");
    expect(parseLogLine(LOG_LINES[4]!)?.level).toBe("WARNING");
  });

  it("xray's own output is not formatted that way, so it stays one plain line", () => {
    expect(parseLogLine("failed to start: invalid user id=***")).toBeNull();
    expect(parseLogLine("")).toBeNull();
  });

  it("shortens a logger name and a timestamp for a phone", () => {
    expect(shortLogger("pi_gw_panel.xray_supervisor.supervisor")).toBe("…supervisor");
    expect(shortLogger("root")).toBe("root");
    expect(shortTime("2023-11-14 22:10:58,003")).toBe("22:10:58,003");
    expect(shortTime("22:10:58")).toBe("22:10:58");
  });
});

describe("empty and truncation copy", () => {
  it("a source that is empty by configuration says why; one that can have content does not claim to be empty", () => {
    expect(emptyMessage("xray-error")).toBe(EMPTY_BY_CONFIG);
    expect(emptyMessage("xray-access")).toBe(EMPTY_BY_CONFIG);
    // logs.tail answers [] for a missing file, an unreadable one and an empty one alike.
    for (const source of ["app", "xray-stderr"]) {
      expect(emptyMessage(source)).toBe("Nothing to show — this log is empty, or the gateway has no file for it yet.");
    }
  });

  it("only the in-memory tail can start mid-line, so only it carries the note", () => {
    expect(truncationNote("xray-stderr")).toBe("the panel keeps the last 8 KB of xray's output, so the first line here can be a fragment");
    for (const source of ["app", "xray-error", "xray-access"]) expect(truncationNote(source)).toBeNull();
  });

  it("the footer says how much of what was loaded is on screen, and what Download would write", () => {
    expect(showingLabel(7, 200, "")).toBe("showing 7 of 200 lines");
    expect(showingLabel(7, 200, "xray")).toBe("showing 7 of 200 lines · filter “xray”");
    expect(downloadNote(7, 200)).toBe("Download writes the 7 lines you are looking at, not all 200");
    expect(beforeLoadMessage(200)).toBe("Press Load to read the last 200 lines.");
  });
});
