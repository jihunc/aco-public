// Next's current preset imports React/import/a11y plugins whose peer contracts
// exclude ESLint10. Use supported TS+Hooks rules; verify accessibility behavior
// through keyboard E2E. No ignored peer conflicts or deprecated core linter.
import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import hooks from 'eslint-plugin-react-hooks';
import globals from 'globals';
export default tseslint.config(
  {ignores: ['.next/**', 'next-env.d.ts']},
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {languageOptions: {globals: {...globals.browser, ...globals.node}},
    plugins: {'react-hooks': hooks}, rules: hooks.configs.recommended.rules},
);
