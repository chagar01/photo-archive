/**
 * photo-archive: trash Google Takeout files that the home machine has downloaded
 * and verified. Runs in your Google account (as you, the files' owner), because
 * the home machine's service account is not allowed to trash files you own.
 *
 * The home machine marks each verified file by setting its description to
 *   "photo-archive verified <time> md5=<md5> size=<bytes>"
 * Only files carrying that mark, with an unchanged size, are trashed. Anything
 * else in the folder (e.g. a new export still arriving) is never touched.
 * Trashed files stay recoverable in Drive's trash for 30 days.
 *
 * Setup (once): paste this into a new project at https://script.google.com,
 * select the function "install" and press Run, then allow access. That creates
 * a daily trigger; check it under Triggers (clock icon). Run
 * "trashVerifiedTakeoutFiles" by hand any time to see what it would do in the log.
 */
const FOLDER_ID = '__FOLDER_ID__';
const TAG = /^photo-archive verified (\S+) md5=([0-9a-f]{32}) size=(\d+)/;

function trashVerifiedTakeoutFiles() {
  const files = DriveApp.getFolderById(FOLDER_ID).getFiles();
  let trashed = 0, kept = 0;
  while (files.hasNext()) {
    const f = files.next();
    const m = TAG.exec(f.getDescription() || '');
    if (!m || f.getSize() !== Number(m[3])) {
      kept++;
      continue;
    }
    f.setTrashed(true);
    trashed++;
    console.log('trashed ' + f.getName());
  }
  console.log('done: trashed ' + trashed + ', left ' + kept + ' unverified file(s) alone');
}

function install() {
  ScriptApp.getProjectTriggers()
    .filter(function (t) { return t.getHandlerFunction() === 'trashVerifiedTakeoutFiles'; })
    .forEach(function (t) { ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger('trashVerifiedTakeoutFiles').timeBased().everyDays(1).atHour(6).create();
  DriveApp.getFolderById(FOLDER_ID);   // fails now (not at 6am) if the ID is wrong
  console.log('daily trigger installed (around 06:00); folder OK');
}
