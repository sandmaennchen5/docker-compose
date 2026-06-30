<?php
$queueDir = "/var/spool/msmtp-queue";
$getmailLog = "/var/log/getmail/getmail.log";
$msmtpLog = "/var/log/getmail/msmtp.log";

function queueCount($dir) {
    if (!is_dir($dir)) return 0;
    return count(scandir($dir)) - 2;
}

function smtpStatus() {
    $result = shell_exec("nc -z synology-smtp 25 >/dev/null 2>&1; echo $?");
    return trim($result) === "0" ? "OK" : "DOWN";
}

$queue = queueCount($queueDir);
$smtp = smtpStatus();
$getmailTail = shell_exec("tail -n 10 $getmailLog");
$msmtpTail = shell_exec("tail -n 10 $msmtpLog");
$errorTail = shell_exec("grep -i 'error' $getmailLog | tail -n 10");
?>
<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<title>Mail Dashboard</title>
<style>
body { font-family: Arial; background:#f4f4f4; padding:20px; }
.box { background:white; padding:20px; margin-bottom:20px; border-radius:8px; }
h2 { margin-top:0; }
.ok { color:green; font-weight:bold; }
.down { color:red; font-weight:bold; }
pre { background:#eee; padding:10px; border-radius:5px; }
</style>
</head>
<body>

<div class="box">
    <h2> Queue</h2>
    <p>Anzahl wartender Mails: <span class="<?= $queue > 10 ? 'down' : 'ok' ?>"><?= $queue ?></span></p>
</div>

<div class="box">
    <h2> SMTP Status</h2>
    <p>Synology SMTP: 
        <span class="<?= $smtp === 'OK' ? 'ok' : 'down' ?>"><?= $smtp ?></span>
    </p>
</div>

<div class="box">
    <h2> Letzte getmailAktivität</h2>
    <pre><?= htmlspecialchars($getmailTail) ?></pre>
</div>

<div class="box">
    <h2> Letzte msmtpAktivität</h2>
    <pre><?= htmlspecialchars($msmtpTail) ?></pre>
</div>

<div class="box">
    <h2> Fehler (getmail)</h2>
    <pre><?= htmlspecialchars($errorTail ?: "Keine Fehler gefunden") ?></pre>
</div>

</body>
</html>
