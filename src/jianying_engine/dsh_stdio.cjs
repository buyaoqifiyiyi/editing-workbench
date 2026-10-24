// DSH 0.2.1-rc.2 account-listing diagnostics use console.info on ACP stdout.
// Keep the official CLI entrypoint/argv and direct JSON-RPC stdout untouched.
console.info = (...args) => console.error(...args);
