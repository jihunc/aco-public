import path from 'node:path';
import {fileURLToPath} from 'node:url';

// Trace the pnpm workspace into a portable production server; public config contains no secrets.
export default {
  output: 'standalone',
  outputFileTracingRoot: path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..'),
};
