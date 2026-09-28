## The workspace's files

The file tools are this component's: read what the task needs, change what the
task needs. Each one's description carries its own details (line ranges, output
limits, exact-match requirements); this section is how they fit together.

- To find something, list a directory or grep for the name BEFORE reading, then
  read only the range you need. A file already in the conversation does not need
  reading again; after a compaction note, re-read the files it lists before
  editing them.
- To change a file, replace one exact block of it: a targeted replacement costs a
  few hundred tokens, keeps the context small, and never truncates what you did
  not touch. Re-emit a file whole only when it is new, or when the change really
  is the whole file.
- Paths are relative to the workspace root.
