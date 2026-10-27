import { describe, it, expect, beforeAll } from "vitest";
import { luaEscape, luaPath, AsepriteMcpServer } from "./index.js";

// ---------------------------------------------------------------------------
// Unit tests — no Aseprite required
// ---------------------------------------------------------------------------

describe("luaEscape", () => {
  it("passes through simple strings unchanged", () => {
    expect(luaEscape("hello")).toBe("hello");
    expect(luaEscape("Layer 1")).toBe("Layer 1");
  });

  it("escapes backslashes", () => {
    expect(luaEscape("a\\b")).toBe("a\\\\b");
    expect(luaEscape("C:\\Users\\test")).toBe("C:\\\\Users\\\\test");
  });

  it("escapes double quotes", () => {
    expect(luaEscape('say "hello"')).toBe('say \\"hello\\"');
  });

  it("escapes newlines and carriage returns", () => {
    expect(luaEscape("line1\nline2")).toBe("line1\\nline2");
    expect(luaEscape("line1\r\nline2")).toBe("line1\\r\\nline2");
  });

  it("escapes combined special characters", () => {
    expect(luaEscape('path\\to\\"file"\n')).toBe('path\\\\to\\\\\\"file\\"\\n');
  });

  it("handles empty string", () => {
    expect(luaEscape("")).toBe("");
  });

  it("blocks Lua injection attempts", () => {
    const malicious = '"; os.execute("rm -rf /"); --';
    const escaped = luaEscape(malicious);
    // All quotes are escaped — can't break out of a Lua string literal
    expect(escaped).toBe('\\"; os.execute(\\"rm -rf /\\"); --');
    // When embedded in Lua as: local s = "<escaped>" the quotes won't terminate the string
  });
});

describe("luaPath", () => {
  it("converts Windows backslashes to forward slashes", () => {
    expect(luaPath("C:\\Users\\test\\sprite.aseprite")).toBe(
      "C:/Users/test/sprite.aseprite",
    );
  });

  it("leaves Unix paths unchanged", () => {
    expect(luaPath("/home/user/sprite.aseprite")).toBe(
      "/home/user/sprite.aseprite",
    );
  });

  it("escapes quotes in paths", () => {
    expect(luaPath('C:\\My "Files"\\test.ase')).toBe(
      'C:/My \\"Files\\"/test.ase',
    );
  });

  it("handles empty path", () => {
    expect(luaPath("")).toBe("");
  });
});

// ---------------------------------------------------------------------------
// AsepriteMcpServer helper method tests
// ---------------------------------------------------------------------------

describe("AsepriteMcpServer", () => {
  let server: AsepriteMcpServer;

  beforeAll(() => {
    server = new AsepriteMcpServer();
  });

  // -- Parameter helpers --

  describe("toSnakeCase", () => {
    it("converts camelCase to snake_case", () => {
      expect(server.toSnakeCase("filePath")).toBe("file_path");
      expect(server.toSnakeCase("layerName")).toBe("layer_name");
      expect(server.toSnakeCase("borderPadding")).toBe("border_padding");
    });

    it("handles already snake_case", () => {
      expect(server.toSnakeCase("file_path")).toBe("file_path");
    });

    it("handles single word", () => {
      expect(server.toSnakeCase("width")).toBe("width");
    });
  });

  describe("toCamelCase", () => {
    it("converts snake_case to camelCase", () => {
      expect(server.toCamelCase("file_path")).toBe("filePath");
      expect(server.toCamelCase("layer_name")).toBe("layerName");
      expect(server.toCamelCase("border_padding")).toBe("borderPadding");
    });

    it("handles already camelCase", () => {
      expect(server.toCamelCase("filePath")).toBe("filePath");
    });

    it("handles single word", () => {
      expect(server.toCamelCase("width")).toBe("width");
    });
  });

  describe("requireParam", () => {
    it("returns value for camelCase key", () => {
      expect(server.requireParam({ filePath: "/test.ase" }, "filePath")).toBe(
        "/test.ase",
      );
    });

    it("returns value for snake_case key when camelCase is requested", () => {
      expect(server.requireParam({ file_path: "/test.ase" }, "filePath")).toBe(
        "/test.ase",
      );
    });

    it("returns value for camelCase key when snake_case is requested", () => {
      expect(server.requireParam({ filePath: "/test.ase" }, "file_path")).toBe(
        "/test.ase",
      );
    });

    it("throws on missing parameter", () => {
      expect(() => server.requireParam({}, "filePath")).toThrow(
        "Missing required parameter: filePath",
      );
    });

    it("throws on empty string", () => {
      expect(() => server.requireParam({ filePath: "" }, "filePath")).toThrow(
        "Missing required parameter: filePath",
      );
    });

    it("throws on null", () => {
      expect(() =>
        server.requireParam({ filePath: null }, "filePath"),
      ).toThrow("Missing required parameter: filePath");
    });

    it("converts numbers to strings", () => {
      expect(server.requireParam({ width: 32 }, "width")).toBe("32");
    });
  });

  describe("optParam", () => {
    it("returns value when present", () => {
      expect(server.optParam({ savePath: "/out.png" }, "savePath")).toBe(
        "/out.png",
      );
    });

    it("returns undefined when missing", () => {
      expect(server.optParam({}, "savePath")).toBeUndefined();
    });

    it("returns undefined for null", () => {
      expect(server.optParam({ savePath: null }, "savePath")).toBeUndefined();
    });

    it("resolves snake_case aliases", () => {
      expect(server.optParam({ save_path: "/out.png" }, "savePath")).toBe(
        "/out.png",
      );
    });
  });

  describe("numParam", () => {
    it("returns number value", () => {
      expect(server.numParam({ width: 64 }, "width")).toBe(64);
    });

    it("parses string as number", () => {
      expect(server.numParam({ width: "64" }, "width")).toBe(64);
    });

    it("returns default when missing", () => {
      expect(server.numParam({}, "width", 32)).toBe(32);
    });

    it("returns undefined when missing with no default", () => {
      expect(server.numParam({}, "width")).toBeUndefined();
    });

    it("resolves snake_case aliases", () => {
      expect(server.numParam({ frame_number: 5 }, "frameNumber")).toBe(5);
    });
  });

  describe("boolParam", () => {
    it("returns boolean value directly", () => {
      expect(server.boolParam({ filled: true }, "filled")).toBe(true);
      expect(server.boolParam({ filled: false }, "filled")).toBe(false);
    });

    it("parses 'true' string", () => {
      expect(server.boolParam({ filled: "true" }, "filled")).toBe(true);
    });

    it("parses '1' string", () => {
      expect(server.boolParam({ filled: "1" }, "filled")).toBe(true);
    });

    it("parses other strings as false", () => {
      expect(server.boolParam({ filled: "false" }, "filled")).toBe(false);
      expect(server.boolParam({ filled: "0" }, "filled")).toBe(false);
    });

    it("returns default when missing", () => {
      expect(server.boolParam({}, "filled", true)).toBe(true);
      expect(server.boolParam({}, "filled", false)).toBe(false);
    });

    it("returns undefined when missing with no default", () => {
      expect(server.boolParam({}, "filled")).toBeUndefined();
    });
  });

  // -- Color conversion --

  describe("luaColor", () => {
    it("handles palette index (number)", () => {
      expect(server.luaColor(5)).toBe("Color(5)");
      expect(server.luaColor(0)).toBe("Color(0)");
    });

    it("handles hex color #RRGGBB", () => {
      expect(server.luaColor("#FF0000")).toBe(
        "Color{ r=255, g=0, b=0, a=255 }",
      );
      expect(server.luaColor("#00FF00")).toBe(
        "Color{ r=0, g=255, b=0, a=255 }",
      );
      expect(server.luaColor("#0000FF")).toBe(
        "Color{ r=0, g=0, b=255, a=255 }",
      );
    });

    it("handles hex color #RRGGBBAA", () => {
      expect(server.luaColor("#FF000080")).toBe(
        "Color{ r=255, g=0, b=0, a=128 }",
      );
    });

    it("handles RGBA object", () => {
      expect(server.luaColor({ r: 128, g: 64, b: 32, a: 200 })).toBe(
        "Color{ r=128, g=64, b=32, a=200 }",
      );
    });

    it("handles RGBA object with missing alpha (defaults to 255)", () => {
      expect(server.luaColor({ r: 128, g: 64, b: 32 })).toBe(
        "Color{ r=128, g=64, b=32, a=255 }",
      );
    });

    it("handles RGBA object with missing components (defaults to 0)", () => {
      expect(server.luaColor({})).toBe("Color{ r=0, g=0, b=0, a=255 }");
    });

    it("handles null/undefined (defaults to black)", () => {
      expect(server.luaColor(null)).toBe("Color{ r=0, g=0, b=0, a=255 }");
      expect(server.luaColor(undefined)).toBe(
        "Color{ r=0, g=0, b=0, a=255 }",
      );
    });

    it("handles string number as Color(n)", () => {
      expect(server.luaColor("42")).toBe("Color(42)");
    });
  });

  // -- Response formatting --

  describe("ok", () => {
    it("wraps string as text content", () => {
      const result = server.ok("success");
      expect(result.content).toHaveLength(1);
      expect(result.content[0].type).toBe("text");
      expect(result.content[0].text).toBe("success");
      expect(result.isError).toBeUndefined();
    });

    it("serializes objects as JSON", () => {
      const result = server.ok({ width: 32, height: 32 });
      expect(result.content[0].text).toBe(
        JSON.stringify({ width: 32, height: 32 }, null, 2),
      );
    });

    it("serializes arrays as JSON", () => {
      const result = server.ok([1, 2, 3]);
      expect(result.content[0].text).toBe(JSON.stringify([1, 2, 3], null, 2));
    });
  });

  describe("error", () => {
    it("returns isError true with message", () => {
      const result = server.error("Something failed");
      expect(result.isError).toBe(true);
      expect(result.content).toHaveLength(1);
      expect(result.content[0].text).toBe("Something failed");
    });

    it("includes solutions when provided", () => {
      const result = server.error("Not found", ["Check path", "Set env var"]);
      expect(result.isError).toBe(true);
      expect(result.content).toHaveLength(2);
      expect(result.content[1].text).toContain("Check path");
      expect(result.content[1].text).toContain("Set env var");
    });

    it("omits solutions block when empty", () => {
      const result = server.error("Fail", []);
      expect(result.content).toHaveLength(1);
    });
  });

  // -- Tool definitions --

  describe("toolDefinitions", () => {
    it("returns exactly 54 tools", () => {
      const tools = server.toolDefinitions();
      expect(tools).toHaveLength(54);
    });

    it("exposes the batch-edit + verification family with their required params", () => {
      const tools = server.toolDefinitions();
      const byName = new Map(tools.map((t) => [t.name, t as { inputSchema: { required?: string[] } }]));
      const expectRequired = (name: string, required: string[]) => {
        const t = byName.get(name);
        expect(t, `${name} should exist`).toBeDefined();
        expect(t!.inputSchema.required).toEqual(required);
      };
      expectRequired("crop_cels", ["filePath"]);
      expectRequired("measure_sprite", ["filePath"]);
      expectRequired("remap_colors", ["filePath", "map"]);
      expectRequired("diff_sprites", ["a", "b"]);
      // crop_cels must NOT require a rect: omitting it means "crop to the content bbox"
      expect(byName.get("crop_cels")!.inputSchema.required).not.toContain("rect");
    });

    it("each tool has name, description, and inputSchema", () => {
      const tools = server.toolDefinitions();
      for (const tool of tools) {
        expect(tool.name).toBeTruthy();
        expect(tool.description).toBeTruthy();
        expect(tool.inputSchema).toBeDefined();
        expect(tool.inputSchema.type).toBe("object");
      }
    });

    it("all tool names are unique", () => {
      const tools = server.toolDefinitions();
      const names = tools.map((t) => t.name);
      expect(new Set(names).size).toBe(names.length);
    });

    it("all tool names follow snake_case convention", () => {
      const tools = server.toolDefinitions();
      for (const tool of tools) {
        expect(tool.name).toMatch(/^[a-z][a-z0-9_]*$/);
      }
    });

    it("required fields are arrays of strings", () => {
      const tools = server.toolDefinitions();
      for (const tool of tools) {
        const schema = tool.inputSchema as { required?: string[] };
        if (schema.required) {
          expect(Array.isArray(schema.required)).toBe(true);
          for (const r of schema.required) {
            expect(typeof r).toBe("string");
          }
        }
      }
    });

    it("contains expected tool categories", () => {
      const tools = server.toolDefinitions();
      const names = tools.map((t) => t.name);

      // Sprite management
      expect(names).toContain("create_sprite");
      expect(names).toContain("open_sprite");
      expect(names).toContain("save_sprite");
      expect(names).toContain("get_sprite_info");
      expect(names).toContain("resize_sprite");
      expect(names).toContain("crop_sprite");

      // Layer management
      expect(names).toContain("add_layer");
      expect(names).toContain("remove_layer");
      expect(names).toContain("list_layers");
      expect(names).toContain("set_layer_properties");

      // Frames & animation
      expect(names).toContain("add_frame");
      expect(names).toContain("remove_frame");
      expect(names).toContain("set_frame_duration");
      expect(names).toContain("list_frames");

      // Tags
      expect(names).toContain("create_tag");
      expect(names).toContain("remove_tag");
      expect(names).toContain("list_tags");

      // Drawing
      expect(names).toContain("draw_pixels");
      expect(names).toContain("draw_rect");
      expect(names).toContain("draw_circle");
      expect(names).toContain("draw_line");
      expect(names).toContain("draw_ellipse");
      expect(names).toContain("fill_area");
      expect(names).toContain("outline");

      // Color & Transform
      expect(names).toContain("replace_color");
      expect(names).toContain("flip_sprite");
      expect(names).toContain("rotate_sprite");
      expect(names).toContain("flatten_layers");
      expect(names).toContain("merge_down");

      // Palette
      expect(names).toContain("get_palette");
      expect(names).toContain("set_palette_colors");
      expect(names).toContain("load_palette");
      expect(names).toContain("resize_palette");

      // Cel
      expect(names).toContain("move_cel");
      expect(names).toContain("set_cel_opacity");
      expect(names).toContain("clear_cel");

      // Export
      expect(names).toContain("export_sprite_sheet");
      expect(names).toContain("export_frame");
      expect(names).toContain("export_layers");

      // Slices
      expect(names).toContain("create_slice");
      expect(names).toContain("remove_slice");

      // Utility
      expect(names).toContain("run_script");
      expect(names).toContain("get_aseprite_version");
    });

    it("create_sprite requires width, height, savePath", () => {
      const tools = server.toolDefinitions();
      const tool = tools.find((t) => t.name === "create_sprite")!;
      const schema = tool.inputSchema as { required: string[] };
      expect(schema.required).toContain("width");
      expect(schema.required).toContain("height");
      expect(schema.required).toContain("savePath");
    });

    it("export_sprite_sheet has sheetType enum", () => {
      const tools = server.toolDefinitions();
      const tool = tools.find((t) => t.name === "export_sprite_sheet")!;
      const props = tool.inputSchema.properties as Record<string, { enum?: string[] }>;
      expect(props.sheetType.enum).toEqual([
        "horizontal",
        "vertical",
        "rows",
        "columns",
        "packed",
      ]);
    });

    it("create_tag has aniDir enum", () => {
      const tools = server.toolDefinitions();
      const tool = tools.find((t) => t.name === "create_tag")!;
      const props = tool.inputSchema.properties as Record<string, { enum?: string[] }>;
      expect(props.aniDir.enum).toEqual([
        "forward",
        "reverse",
        "pingpong",
        "pingpong_reverse",
      ]);
    });

    it("draw_line has thickness parameter", () => {
      const tools = server.toolDefinitions();
      const tool = tools.find((t) => t.name === "draw_line")!;
      const props = tool.inputSchema.properties as Record<string, { type?: string }>;
      expect(props.thickness).toBeDefined();
      expect(props.thickness.type).toBe("number");
    });

    it("draw_rect has thickness parameter", () => {
      const tools = server.toolDefinitions();
      const tool = tools.find((t) => t.name === "draw_rect")!;
      const props = tool.inputSchema.properties as Record<string, { type?: string }>;
      expect(props.thickness).toBeDefined();
      expect(props.thickness.type).toBe("number");
    });

    it("draw_circle has thickness parameter", () => {
      const tools = server.toolDefinitions();
      const tool = tools.find((t) => t.name === "draw_circle")!;
      const props = tool.inputSchema.properties as Record<string, { type?: string }>;
      expect(props.thickness).toBeDefined();
      expect(props.thickness.type).toBe("number");
    });

    it("fill_area requires x, y, color", () => {
      const tools = server.toolDefinitions();
      const tool = tools.find((t) => t.name === "fill_area")!;
      const schema = tool.inputSchema as { required: string[] };
      expect(schema.required).toContain("filePath");
      expect(schema.required).toContain("x");
      expect(schema.required).toContain("y");
      expect(schema.required).toContain("color");
    });

    it("fill_area has tolerance and contiguous params", () => {
      const tools = server.toolDefinitions();
      const tool = tools.find((t) => t.name === "fill_area")!;
      const props = tool.inputSchema.properties as Record<string, { type?: string }>;
      expect(props.tolerance).toBeDefined();
      expect(props.tolerance.type).toBe("number");
      expect(props.contiguous).toBeDefined();
      expect(props.contiguous.type).toBe("boolean");
    });

    it("draw_ellipse requires bounds and color", () => {
      const tools = server.toolDefinitions();
      const tool = tools.find((t) => t.name === "draw_ellipse")!;
      const schema = tool.inputSchema as { required: string[] };
      expect(schema.required).toContain("filePath");
      expect(schema.required).toContain("x");
      expect(schema.required).toContain("y");
      expect(schema.required).toContain("width");
      expect(schema.required).toContain("height");
      expect(schema.required).toContain("color");
    });

    it("replace_color requires filePath, fromColor, toColor", () => {
      const tools = server.toolDefinitions();
      const tool = tools.find((t) => t.name === "replace_color")!;
      const schema = tool.inputSchema as { required: string[] };
      expect(schema.required).toContain("filePath");
      expect(schema.required).toContain("fromColor");
      expect(schema.required).toContain("toColor");
    });

    it("flip_sprite has direction enum", () => {
      const tools = server.toolDefinitions();
      const tool = tools.find((t) => t.name === "flip_sprite")!;
      const props = tool.inputSchema.properties as Record<string, { enum?: string[] }>;
      expect(props.direction.enum).toEqual(["horizontal", "vertical"]);
    });

    it("rotate_sprite has angle enum", () => {
      const tools = server.toolDefinitions();
      const tool = tools.find((t) => t.name === "rotate_sprite")!;
      const props = tool.inputSchema.properties as Record<string, { enum?: number[] }>;
      expect(props.angle.enum).toEqual([90, 180, 270]);
    });

    it("flatten_layers requires only filePath", () => {
      const tools = server.toolDefinitions();
      const tool = tools.find((t) => t.name === "flatten_layers")!;
      const schema = tool.inputSchema as { required: string[] };
      expect(schema.required).toEqual(["filePath"]);
    });

    it("merge_down requires filePath and layerName", () => {
      const tools = server.toolDefinitions();
      const tool = tools.find((t) => t.name === "merge_down")!;
      const schema = tool.inputSchema as { required: string[] };
      expect(schema.required).toContain("filePath");
      expect(schema.required).toContain("layerName");
    });

    it("outline requires filePath and color", () => {
      const tools = server.toolDefinitions();
      const tool = tools.find((t) => t.name === "outline")!;
      const schema = tool.inputSchema as { required: string[] };
      expect(schema.required).toContain("filePath");
      expect(schema.required).toContain("color");
    });
  });
});
