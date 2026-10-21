// `law api` for TypeScript/JavaScript projects (Node, Angular, React, React Native): resolves a module the way the
// project's own build does (node_modules, package "exports" maps, re-exports) with the TypeScript compiler, and
// prints the declarations of one export from the packages' .d.ts files.
//
// Usage: node ts_api.mjs <typescript-lib-dir> <project-root> <query> [member]
//   query: "module:Name" (e.g. "@angular/common/http:HttpClient") or "Name" (searched in the direct dependencies
//   and their entry points). Output is JSON: {found: [{module, file, text}], near: [names], error}.
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";

const [tsDir, root, query, member] = process.argv.slice(2);
const ts = createRequire(path.join(tsDir, "package.json"))(tsDir);
const out = (o) => process.stdout.write(JSON.stringify(o));

function entryModules() {
  // Direct dependencies and their public subpath entry points ("@angular/common" -> also "@angular/common/http").
  const pkg = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
  const deps = Object.keys({ ...pkg.dependencies, ...pkg.devDependencies, ...pkg.peerDependencies });
  const mods = [];
  for (const dep of deps) {
    const name = dep.startsWith("@types/") ? dep.slice(7).replace(/^(.+)__(.+)$/, "@$1/$2") : dep;
    mods.push(name);
    const manifest = path.join(root, "node_modules", dep, "package.json");
    if (!fs.existsSync(manifest)) continue;
    const exp = JSON.parse(fs.readFileSync(manifest, "utf8")).exports;
    if (exp && typeof exp === "object" && !Array.isArray(exp)) {
      for (const key of Object.keys(exp)) {
        if (key.startsWith("./") && key !== "./package.json" && !key.includes("*")) mods.push(name + key.slice(1));
      }
    }
  }
  return [...new Set(mods)];
}

function program(modules) {
  const file = path.join(root, "__law_api__.ts");
  const text = modules.map((m, i) => `import * as m${i} from ${JSON.stringify(m)};`).join("\n");
  const options = {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, moduleResolution: ts.ModuleResolutionKind.Bundler,
    jsx: ts.JsxEmit.ReactJSX, skipLibCheck: true, noEmit: true, allowJs: false, strict: true, types: [],
  };
  const host = ts.createCompilerHost(options, true);
  const getSourceFile = host.getSourceFile;
  host.getSourceFile = (name, lang, ...rest) =>
    name === file ? ts.createSourceFile(file, text, lang, true) : getSourceFile.call(host, name, lang, ...rest);
  host.fileExists = ((exists) => (name) => name === file || exists(name))(host.fileExists);
  const prog = ts.createProgram([file], options, host);
  const checker = prog.getTypeChecker();
  const source = prog.getSourceFile(file);
  const moduleSymbols = source.statements.map((s) => checker.getSymbolAtLocation(s.moduleSpecifier));
  return { checker, moduleSymbols };
}

function declarationText(decl, memberFilter) {
  const sf = decl.getSourceFile();
  const file = path.relative(root, sf.fileName);
  if (memberFilter && decl.members) {
    const head = sf.text.slice(decl.getStart(), decl.members.pos).trim();
    const hits = decl.members
      .filter((m) => m.name && m.name.getText().toLowerCase().includes(memberFilter.toLowerCase()))
      .map((m) => "  " + m.getText().replace(/\s+/g, " "));
    return { file, text: [head, ...(hits.length ? hits : [`  (no member mentions '${memberFilter}')`]), "}"].join("\n") };
  }
  return { file, text: decl.getText() };
}

function describe(checker, sym, mod) {
  const target = sym.flags & ts.SymbolFlags.Alias ? checker.getAliasedSymbol(sym) : sym;
  return (target.declarations || []).map((d) => ({ module: mod, ...declarationText(d, member) }));
}

try {
  const sep = query.lastIndexOf(":");
  const explicit = sep > 0 ? query.slice(0, sep) : null;
  const name = explicit ? query.slice(sep + 1) : query;
  const modules = explicit ? [explicit] : entryModules();
  const { checker, moduleSymbols } = program(modules);
  const found = [];
  const all = new Set();
  const missing = [];
  moduleSymbols.forEach((ms, i) => {
    if (!ms) { missing.push(modules[i]); return; }
    for (const e of checker.getExportsOfModule(ms)) {
      all.add(e.name);
      if (e.name === name) found.push(...describe(checker, e, modules[i]));
    }
  });
  out({ found: found.slice(0, 6), names: [...all], missing });
} catch (err) {
  out({ error: String(err && err.stack ? err.stack : err) });
}
