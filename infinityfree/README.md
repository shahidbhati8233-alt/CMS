# CMS Café — InfinityFree PHP/MySQL starter

This is a separate PHP starter for InfinityFree; it does not replace the Flask app yet.

## Install
1. In phpMyAdmin select your empty cafeapp database and choose Import.
2. Import schema.sql.
3. In InfinityFree File Manager open htdocs. Upload all files in this folder.
4. Open config.example.php, fill the four DB values from MySQL Databases, and save the configured copy as config.php in htdocs. Do not upload/share real credentials publicly.
5. Open index.php on your domain and test signup/login.

Scope: account creation and login plus initial MySQL schema only. Menu editor, customer ordering, QR manager, and order dashboard are not yet ported from Flask and must not be treated as complete.
