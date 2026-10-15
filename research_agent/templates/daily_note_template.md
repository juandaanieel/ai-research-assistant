---
type: daily
date: "{{date:YYYY-MM-DD}}"
---
# {{date:YYYY-MM-DD}}

## Main outcome

## Secondary outcome

## Personal outcome

## Scheduled tasks

```tasks
path includes Research/Projects/
scheduled on {{query.file.filenameWithoutExtension}}
```

## Unfinished tasks

```tasks
not done
path includes Research/Projects/
scheduled before {{query.file.filenameWithoutExtension}}
```

[[Daily/AI/AI {{date:YYYY-MM-DD}}|AI log]]
