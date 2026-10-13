#!/bin/sh
# Package the workflow for release: ./build.sh  →  Alfred-RSS.alfredworkflow
cd "$(dirname "$0")/src" && rm -f ../Alfred-RSS.alfredworkflow && zip -q -r ../Alfred-RSS.alfredworkflow info.plist rss.py icon.png images -x '*.DS_Store' && echo "Built Alfred-RSS.alfredworkflow"
