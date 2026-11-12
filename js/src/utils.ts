// SPDX-License-Identifier: Apache-2.0

import { diag, type DiagLogger } from '@opentelemetry/api';

export const logger: DiagLogger = diag.createComponentLogger({
  namespace: 'otel-muzzle',
});
