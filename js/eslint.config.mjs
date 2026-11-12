// SPDX-License-Identifier: Apache-2.0
// Ported from opentelemetry-js-contrib @ c9ddfed6ee4cd1ca0e9430485b5b73289400f436 (eslint.config.mjs)

import eslint from '@eslint/js';
import tseslint from 'typescript-eslint';
import globals from 'globals';
import nodePlugin from 'eslint-plugin-n';
import prettierRecommended from 'eslint-plugin-prettier/recommended';
import yalhPlugin from 'eslint-plugin-yet-another-license-header';

const license = '// SPDX-License-Identifier: Apache-2.0';

export default tseslint.config(
  {
    ignores: ['build/**', 'coverage/**', '.nyc_output/**', 'node_modules/**'],
  },
  {
    files: ['**/*.ts'],
    plugins: {
      'yet-another-license-header': yalhPlugin,
      n: nodePlugin,
    },
    extends: [eslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2022,
      globals: {
        ...globals.node,
      },
    },
    settings: {
      n: {
        tryExtensions: ['.js', '.json', '.node', '.ts', '.tsx'],
      },
    },
    rules: {
      quotes: ['error', 'single', { avoidEscape: true }],
      eqeqeq: ['error', 'smart'],
      'prefer-rest-params': 'off',
      'yet-another-license-header/header': [
        'error',
        { header: license, allowedHeaderPatterns: [] },
      ],
      'no-unused-vars': 'warn',
      'n/no-deprecated-api': 'warn',
    },
  },

  // TypeScript strict rules
  {
    files: ['**/*.ts', '**/*.mts'],
    plugins: {
      '@typescript-eslint': tseslint.plugin,
    },
    extends: [...tseslint.configs.recommended],
    languageOptions: {
      parser: tseslint.parser,
      parserOptions: {
        project: true,
      },
    },
    rules: {
      '@typescript-eslint/no-explicit-any': 'warn',
      '@typescript-eslint/no-require-imports': 'warn',
      '@typescript-eslint/no-unsafe-function-type': 'warn',
      '@typescript-eslint/no-empty-object-type': 'warn',
      '@typescript-eslint/no-unused-vars': [
        'warn',
        { argsIgnorePattern: '^_' },
      ],
      '@typescript-eslint/no-restricted-types': [
        'warn',
        {
          types: {
            Function: 'fix',
          },
        },
      ],
      '@typescript-eslint/no-floating-promises': 'error',
      '@typescript-eslint/no-this-alias': 'off',
      '@typescript-eslint/naming-convention': [
        'error',
        {
          selector: 'memberLike',
          modifiers: ['private', 'protected'],
          format: ['camelCase'],
          leadingUnderscore: 'require',
        },
      ],
      '@typescript-eslint/no-var-requires': 'off',
      '@typescript-eslint/no-inferrable-types': [
        'error',
        { ignoreProperties: true },
      ],
      '@typescript-eslint/no-empty-function': ['off'],
      '@typescript-eslint/no-shadow': ['warn'],
      '@typescript-eslint/explicit-member-accessibility': [
        'error',
        {
          overrides: {
            // `constructor` is public by default.
            constructors: 'no-public',
          },
        },
      ],
    },
  },

  // Tests use relaxed rules.
  {
    files: ['test/**/*.ts', '**/*.test.ts'],
    rules: {
      '@typescript-eslint/no-explicit-any': 'off',
      '@typescript-eslint/no-non-null-assertion': 'off',
      '@typescript-eslint/no-shadow': 'off',
      '@typescript-eslint/no-floating-promises': 'off',
      '@typescript-eslint/explicit-member-accessibility': 'off',
      'no-empty': 'off',
    },
  },

  // ESM files
  {
    files: ['**/*.mjs', '**/*.mts'],
    languageOptions: {
      sourceType: 'module',
    },
  },

  prettierRecommended
);
