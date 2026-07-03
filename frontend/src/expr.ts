/** Safe expression evaluator — 1:1 mirror of blender/builders/expr.py.
 * Grammar: numbers, parameter/toggle ids, + - * /, unary minus, parens,
 * min()/max()/abs(). Nothing else, so LLM specs can't execute code. */

const FUNCS: Record<string, (...xs: number[]) => number> = {
  min: Math.min,
  max: Math.max,
  abs: Math.abs,
};

const TOKEN = /\s*(?:([A-Za-z_][A-Za-z0-9_]*)|(\d+\.?\d*|\.\d+)|([()+\-*/,]))/y;

export class ExprError extends Error {}

export function evalExpr(
  input: number | string,
  env: Record<string, number>,
): number {
  if (typeof input === "number") {
    if (!Number.isFinite(input)) throw new ExprError(`Non-finite number`);
    return input;
  }
  if (typeof input !== "string") {
    throw new ExprError(`Expected number or expression, got ${typeof input}`);
  }

  // tokenize
  const tokens: Array<{ kind: "ident" | "num" | "op"; value: string }> = [];
  TOKEN.lastIndex = 0;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = TOKEN.exec(input)) !== null) {
    if (m[1]) tokens.push({ kind: "ident", value: m[1] });
    else if (m[2]) tokens.push({ kind: "num", value: m[2] });
    else tokens.push({ kind: "op", value: m[3] });
    last = TOKEN.lastIndex;
  }
  if (last < input.trimEnd().length) {
    throw new ExprError(`Invalid expression ${JSON.stringify(input)}`);
  }

  let pos = 0;
  const peek = () => tokens[pos];
  const take = () => tokens[pos++];
  const expectOp = (op: string) => {
    const t = take();
    if (!t || t.kind !== "op" || t.value !== op) {
      throw new ExprError(`Expected '${op}' in ${JSON.stringify(input)}`);
    }
  };

  function parseExpr(): number {
    let left = parseTerm();
    while (peek()?.kind === "op" && (peek().value === "+" || peek().value === "-")) {
      const op = take().value;
      const right = parseTerm();
      left = op === "+" ? left + right : left - right;
    }
    return left;
  }

  function parseTerm(): number {
    let left = parseFactor();
    while (peek()?.kind === "op" && (peek().value === "*" || peek().value === "/")) {
      const op = take().value;
      const right = parseFactor();
      left = op === "*" ? left * right : left / right;
    }
    return left;
  }

  function parseFactor(): number {
    const t = take();
    if (!t) throw new ExprError(`Unexpected end of expression ${JSON.stringify(input)}`);
    if (t.kind === "num") return parseFloat(t.value);
    if (t.kind === "op" && t.value === "-") return -parseFactor();
    if (t.kind === "op" && t.value === "+") return parseFactor();
    if (t.kind === "op" && t.value === "(") {
      const v = parseExpr();
      expectOp(")");
      return v;
    }
    if (t.kind === "ident") {
      if (peek()?.kind === "op" && peek().value === "(") {
        const fn = FUNCS[t.value];
        if (!fn) throw new ExprError(`Unknown function ${t.value}()`);
        take(); // (
        const args = [parseExpr()];
        while (peek()?.kind === "op" && peek().value === ",") {
          take();
          args.push(parseExpr());
        }
        expectOp(")");
        return fn(...args);
      }
      const v = env[t.value];
      if (v === undefined) {
        throw new ExprError(
          `Unknown name '${t.value}' (known: ${Object.keys(env).join(", ") || "<none>"})`,
        );
      }
      return v;
    }
    throw new ExprError(`Unexpected token '${t.value}' in ${JSON.stringify(input)}`);
  }

  const result = parseExpr();
  if (pos !== tokens.length) {
    throw new ExprError(`Trailing tokens in ${JSON.stringify(input)}`);
  }
  if (!Number.isFinite(result)) {
    throw new ExprError(`Expression ${JSON.stringify(input)} is not finite`);
  }
  return result;
}
