// Execute existing declarations, never the HTML's startup code or browser actions.
const vm = require('node:vm');
const fs = require('node:fs');
const { createRequire } = require('node:module');
const ts = createRequire(process.argv[2])('typescript');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const declarations = [];
for (const script of input.scripts) {
  const ast = ts.createSourceFile('legacy.js', script, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
  for (const node of ast.statements) {
    if (ts.isFunctionDeclaration(node)) declarations.push(node.getText(ast));
    if (ts.isVariableStatement(node)) {
      for (const declaration of node.declarationList.declarations) {
        if (declaration.name.getText(ast) === 'ORDERS_STATUS_OPTIONS') {
          declarations.push('const ' + declaration.getText(ast) + ';');
        }
      }
    }
  }
}
const storage = new Map();
const requests = [];
const context = vm.createContext({
  document: { getElementById: () => null },
  window: {
    location: { pathname: '/orders/print' },
    localStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) },
    print: () => { throw new Error('Physical printing forbidden'); },
  },
  fetch: async (url, options) => {
    requests.push({ url, method: options?.method });
    throw new Error('Synthetic transport failure; no network');
  },
  ordersPrintSource: 'avito', ordersSelectedDate: '2026-09-09', ordersArchiveMode: false,
  ordersPrintPayload: {}, ORDERS_PRINT_FALLBACK: { date: '2026-09-09' },
});
vm.runInContext(declarations.join('\n'), context, { timeout: 5000 });
// UI-only boundaries: the tested send logic still performs its original catch path.
context.getOrdersActiveSource = () => 'avito';
context.renderOrdersDeliveryState = () => {};
context.showToast = () => {};
(async () => {
  const results = [];
  for (const call of input.calls) {
    if (call.archive !== undefined) context.ordersArchiveMode = call.archive;
    if (typeof context[call.name] !== 'function') throw new Error('Missing executable function');
    results.push(await context[call.name](...call.args));
  }
  process.stdout.write(JSON.stringify({ results, storage: Object.fromEntries(storage), requests }));
})().catch(() => { process.stderr.write('Synthetic characterization failed\n'); process.exitCode = 1; });
