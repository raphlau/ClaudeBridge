# Claude Bridge: privacy

Claude Bridge is a personal tool. It is a small server that runs on its owner's own computer and lets the owner's
Claude assistant reach his own documents, notes and Google Tasks. It isn't a service offered to anyone else. Each
install uses its owner's own Google OAuth client and is used only with his own Google account.

## Google user data

- **What it accesses:** only with the owner's permission, his Google Tasks lists and tasks, through the Google Tasks
  API (`https://www.googleapis.com/auth/tasks`). It reads them, and adds, changes or deletes them, when his Claude
  assistant asks for that on his behalf.
- **What it keeps:** the sign-in (an OAuth refresh token) and the OAuth client, in one file on the owner's own
  computers that no tool of the bridge can show. Short-lived access tokens are kept in memory only. The bridge keeps no
  copy of his tasks, and its log records which tool ran but not task titles or notes.
- **Where the data goes:** only between Google, the owner's computer and his own Claude assistant. It isn't sold,
  shared with anyone else, used for advertising, or used to train AI models.
- The use of information received from Google APIs follows the
  [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy), including
  the Limited Use requirements.

## Stopping access

The owner can sign the bridge out at any time (ask Claude to disconnect Google Tasks, which also revokes the sign-in at
Google), or remove its access at [myaccount.google.com/permissions](https://myaccount.google.com/permissions).

## Contact

Open an issue on this repository: https://github.com/raphlau/ClaudeBridge/issues
