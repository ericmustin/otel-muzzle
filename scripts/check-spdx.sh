#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
#
# Fails when a tracked source or build file lacks the license identifier in its first three lines.
# bench/vendor/ is third-party and excluded.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

# Files derived from third-party code outside bench/vendor/ keep their upstream license. Each passes with
# SPDX-License-Identifier: MIT in its first five lines, instead of Apache-2.0 in its first three.
mit_files=(
  bench/gitlab/opentelemetry.rb
)

missing=0
while IFS= read -r file; do
  [ -f "$file" ] || continue
  lines=3
  id='Apache-2.0'
  for mit in "${mit_files[@]}"; do
    if [ "$file" = "$mit" ]; then lines=5; id='MIT'; fi
  done
  if ! head -n "$lines" "$file" | grep -q "SPDX-License-Identifier: $id"; then
    echo "missing SPDX header ($id): $file"
    missing=1
  fi
done < <(git ls-files -- '*.py' '*.rb' '*.sh' '*.ts' '*.mts' '*.js' '*.mjs' '*.cjs' \
  'Gemfile' '*/Gemfile' 'Rakefile' '*/Rakefile' 'Appraisals' '*/Appraisals' '*.gemspec' ':!:bench/vendor/*')
exit "$missing"
