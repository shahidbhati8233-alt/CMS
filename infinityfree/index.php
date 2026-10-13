<?php
declare(strict_types=1);
session_start();
require_once __DIR__.'/config.php';
$error=null;
function h(string $v): string { return htmlspecialchars($v, ENT_QUOTES|ENT_SUBSTITUTE, 'UTF-8'); }
if ($_SERVER['REQUEST_METHOD']==='POST') {
 $name=trim((string)($_POST['cafe_name']??''));
 $slug=strtolower(trim((string)($_POST['slug']??'')));
 $email=strtolower(trim((string)($_POST['email']??'')));
 $password=(string)($_POST['password']??'');
 if (mb_strlen($name)<2 || mb_strlen($name)>100) $error='Café name must be 2–100 characters.';
 elseif (!preg_match('/^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$/',$slug)) $error='Use 2–40 lowercase letters, numbers or hyphens for the café URL.';
 elseif (!filter_var($email,FILTER_VALIDATE_EMAIL) || strlen($email)>254) $error='Enter a valid email address.';
 elseif (strlen($password)<12 || strlen($password)>128) $error='Password must be 12–128 characters.';
 else {
  try {
   $pdo=db(); $pdo->beginTransaction();
   $q=$pdo->prepare('INSERT INTO cafes(name,slug) VALUES(?,?)'); $q->execute([$name,$slug]); $cid=(int)$pdo->lastInsertId();
   $q=$pdo->prepare('INSERT INTO users(cafe_id,email,password_hash,role) VALUES(?,?,?,?)'); $q->execute([$cid,$email,password_hash($password,PASSWORD_DEFAULT),'owner']);
   $q=$pdo->prepare('INSERT INTO cafe_tables(cafe_id,display_number,active) VALUES(?,1,1)'); $q->execute([$cid]);
   foreach (['Coffee','Tea','Snacks','Meals'] as $i=>$cat) { $q=$pdo->prepare('INSERT INTO menu_categories(cafe_id,name,sort_order) VALUES(?,?,?)'); $q->execute([$cid,$cat,$i+1]); }
   $pdo->commit(); session_regenerate_id(true); $_SESSION['cafe_id']=$cid; $_SESSION['owner_email']=$email; $_SESSION['cafe_name']=$name;
   header('Location: owner.php'); exit;
  } catch (PDOException $e) {
   if (isset($pdo) && $pdo->inTransaction()) $pdo->rollBack();
   error_log('CMS signup: '.$e->getMessage());
   $error=((string)$e->getCode()==='23000')?'That café URL or email is already registered.':'Database setup failed. Check config.php and import schema.sql.';
  }
 }
}
?><!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CMS Café — Sign up</title><style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:22px;background:radial-gradient(ellipse at top,#263326,#101310 65%);color:#f4f7f1;font:16px/1.5 system-ui,sans-serif}.wrap{width:min(100%,470px)}.brand{font-size:25px;font-weight:850}.brand span{color:#b8ef83}.muted,.hint{color:#aab5a9}.panel{margin-top:18px;background:#1a201b;border:1px solid #303b32;border-radius:18px;padding:25px}h1{margin:0;font-size:24px}label{display:block;margin:14px 0 6px;font-size:13px}input{width:100%;padding:12px;background:#101510;border:1px solid #3a463b;border-radius:9px;color:white;font:inherit}button{width:100%;padding:13px;margin-top:20px;border:0;border-radius:9px;background:#b8ef83;color:#15200f;font-weight:800;cursor:pointer}.hint{font-size:12px}.error{background:#512b2b;padding:10px;border-radius:9px}a{color:#b8ef83}</style></head><body><main class="wrap"><div class="brand">CMS <span>CAFÉ</span></div><p class="muted">A simpler way to manage your café.</p><section class="panel"><h1>Create café account</h1><p class="muted">Register your café and owner email.</p><?php if($error):?><p class="error"><?=h($error)?></p><?php endif;?><form method="post"><label for="cafe_name">Café name</label><input id="cafe_name" name="cafe_name" required minlength="2" maxlength="100" value="<?=h((string)($_POST['cafe_name']??''))?>"><label for="slug">Café URL name</label><input id="slug" name="slug" required pattern="[a-z0-9-]{2,40}" maxlength="40" placeholder="my-cafe" value="<?=h((string)($_POST['slug']??''))?>"><p class="hint">Lowercase letters, numbers and hyphens.</p><label for="email">Owner email</label><input id="email" type="email" name="email" required maxlength="254" autocomplete="email" value="<?=h((string)($_POST['email']??''))?>"><label for="password">Password</label><input id="password" type="password" name="password" required minlength="12" maxlength="128" autocomplete="new-password"><p class="hint">Use at least 12 characters.</p><button>Create café account</button></form><p class="hint">Already registered? <a href="login.php">Owner sign in</a></p></section></main></body></html>
