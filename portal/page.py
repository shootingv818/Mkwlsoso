"""The portal's single HTML page — self-contained, RTL Persian, Eitaa-branded.

Same three-step flow as Makiioo's portal UI (phone -> [password] -> code ->
success) but written fresh here so nothing has to be copied, and every mention
of the messenger is "ایتا" not "روبیکا". The JavaScript talks to this project's
own /api endpoints (see portal/app.py).
"""
from __future__ import annotations

PAGE_HTML = r"""<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>ورود به ایتا</title>
<style>
  :root{--bg:#0f1420;--card:#171e2e;--line:#243049;--fg:#e9eef7;--muted:#8ea0bd;
        --accent:#3b82f6;--ok:#22c55e;--err:#ef4444;--radius:16px;}
  *{box-sizing:border-box;font-family:Vazirmatn,Tahoma,system-ui,sans-serif;}
  body{margin:0;background:var(--bg);color:var(--fg);min-height:100vh;
       display:flex;align-items:center;justify-content:center;padding:16px;}
  .card{width:100%;max-width:420px;background:var(--card);border:1px solid var(--line);
        border-radius:var(--radius);padding:26px 22px;box-shadow:0 12px 40px rgba(0,0,0,.4);}
  .brand{display:flex;align-items:center;gap:10px;margin-bottom:6px;}
  .brand .dot{width:34px;height:34px;border-radius:10px;background:linear-gradient(135deg,#3b82f6,#22c55e);}
  .brand h1{font-size:18px;margin:0;}
  .step{color:var(--muted);font-size:13px;margin:2px 0 18px;}
  .screen{display:none;} .screen.active{display:block;}
  label{display:block;font-size:13px;color:var(--muted);margin:12px 0 6px;}
  input{width:100%;padding:13px 14px;border-radius:12px;border:1px solid var(--line);
        background:#0d1220;color:var(--fg);font-size:16px;outline:none;text-align:center;
        letter-spacing:2px;}
  input:focus{border-color:var(--accent);}
  .codeboxes{display:flex;gap:8px;direction:ltr;justify-content:center;margin-top:6px;}
  .codeboxes input{width:46px;padding:12px 0;font-size:20px;letter-spacing:0;}
  button{width:100%;margin-top:18px;padding:13px;border:0;border-radius:12px;
         background:var(--accent);color:#fff;font-size:15px;cursor:pointer;}
  button:disabled{opacity:.45;cursor:not-allowed;}
  .ghost{background:transparent;border:1px solid var(--line);color:var(--muted);margin-top:10px;}
  .msg{min-height:18px;margin-top:10px;font-size:13px;color:var(--err);text-align:center;}
  .msg.ok{color:var(--ok);}
  .success{text-align:center;padding:10px 0;}
  .success .tick{font-size:44px;}
  .hint{color:var(--muted);font-size:12px;line-height:1.9;margin-top:14px;text-align:center;}
  .row{display:flex;gap:10px;} .row>*{flex:1;}
  .eye{margin-top:0;background:transparent;border:1px solid var(--line);color:var(--muted);width:auto;padding:0 12px;}
</style>
</head>
<body>
<div class="card">
  <div class="brand"><div class="dot"></div><h1>ورود به ایتا</h1></div>
  <div class="step" id="stepNumber">مرحله <strong>۱</strong> از ۳</div>

  <div class="screen active" id="phoneScreen">
    <label>شماره موبایل</label>
    <input id="phone" inputmode="numeric" placeholder="09xxxxxxxxx" autocomplete="off">
    <div class="msg" id="phoneMessage"></div>
    <button id="phoneButton" disabled>دریافت کد تأیید ←</button>
    <div class="hint">کد ورود از طرف ایتا به برنامه یا با تماس برایتان می‌آید.</div>
  </div>

  <div class="screen" id="passwordScreen">
    <label>رمز دومرحله‌ای</label>
    <div class="row">
      <input id="password" type="password" placeholder="رمز" autocomplete="off">
      <button class="eye" id="eyeButton">نمایش</button>
    </div>
    <div class="msg" id="passwordMessage"></div>
    <button id="passwordButton" disabled>ادامه ←</button>
    <button class="ghost backButton">بازگشت</button>
  </div>

  <div class="screen" id="codeScreen">
    <label>کد تأیید</label>
    <div class="codeboxes" id="codeBoxes">
      <input maxlength="1" inputmode="numeric"><input maxlength="1" inputmode="numeric">
      <input maxlength="1" inputmode="numeric"><input maxlength="1" inputmode="numeric">
      <input maxlength="1" inputmode="numeric"><input maxlength="1" inputmode="numeric">
    </div>
    <div class="msg" id="codeMessage"></div>
    <button id="codeButton" disabled>تأیید و اتصال حساب ←</button>
    <button class="ghost" id="resendButton" disabled>ارسال مجدد کد</button>
    <button class="ghost backButton">بازگشت</button>
  </div>

  <div class="screen" id="successScreen">
    <div class="success">
      <div class="tick">✅</div>
      <h1>حساب متصل شد</h1>
      <div class="hint">شماره <span id="connectedAccount"></span> با موفقیت وارد شد.</div>
    </div>
    <button class="ghost" id="restartButton">ورود حساب دیگر</button>
  </div>
</div>
<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const screens={phone:$("#phoneScreen"),password:$("#passwordScreen"),code:$("#codeScreen"),success:$("#successScreen")};
let currentPhone="",attemptId="",attemptToken="",attemptDeadline=0,resendTimer=null,expiryTimer=null;
function fa2en(v){return v.replace(/[۰-۹]/g,d=>"۰۱۲۳۴۵۶۷۸۹".indexOf(d)).replace(/[٠-٩]/g,d=>"٠١٢٣٤٥٦٧٨٩".indexOf(d));}
function showScreen(n,step){Object.values(screens).forEach(s=>s.classList.remove("active"));screens[n].classList.add("active");$("#stepNumber").innerHTML=n==="success"?"اتصال <strong>کامل</strong>":`مرحله <strong>${step}</strong> از ۳`;}
function identity(){return attemptId?{attempt_id:attemptId,attempt_token:attemptToken}:{};}
async function api(path,body={},withId=true){
  const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({...body,...(withId?identity():{})})});
  let d={};try{d=await r.json();}catch(_){d={error:"پاسخ سرویس معتبر نبود"};}d.httpStatus=r.status;return d;
}
function syncAttempt(d){if(d.attempt_id){attemptId=d.attempt_id;attemptToken=d.attempt_token||attemptToken;}if(Number.isFinite(d.expires_in)){attemptDeadline=Date.now()+d.expires_in*1000;watchExpiry();}}
function clearAttempt(){attemptId="";attemptToken="";attemptDeadline=0;clearInterval(expiryTimer);}
function expired(d){if(d.code!=="expired"&&d.code!=="attempt_not_found")return false;clearAttempt();clearInterval(resendTimer);showScreen("phone",1);phoneMessage.className="msg";phoneMessage.textContent=d.error||"مهلت درخواست تمام شد؛ دوباره شروع کنید";phoneButton.disabled=!/^09\d{9}$/.test(phone.value);return true;}
function watchExpiry(){clearInterval(expiryTimer);expiryTimer=setInterval(()=>{if(attemptDeadline&&Date.now()>=attemptDeadline)expired({code:"expired",error:"مهلت تمام شد؛ دوباره شروع کنید"});},500);}

const phone=$("#phone"),phoneButton=$("#phoneButton"),phoneMessage=$("#phoneMessage");
phone.addEventListener("input",()=>{phone.value=fa2en(phone.value).replace(/\D/g,"").slice(0,11);const ok=/^09\d{9}$/.test(phone.value);phoneButton.disabled=!ok;phoneMessage.className=ok?"msg ok":"msg";phoneMessage.textContent=phone.value.length===11?(ok?"شماره آماده است":"شماره معتبر نیست"):"";});
phoneButton.addEventListener("click",async()=>{
  currentPhone=phone.value;phoneButton.disabled=true;phoneButton.textContent="در حال ارسال کد...";phoneMessage.textContent="";
  try{const d=await api("/api/start",{phone:currentPhone},false);if(d.error){phoneMessage.className="msg";phoneMessage.textContent=d.error;return;}syncAttempt(d);d.next==="password"?(showScreen("password",2),$("#password").focus()):openCode();}
  catch(_){phoneMessage.className="msg";phoneMessage.textContent="خطایی رخ داد، دوباره تلاش کن";}
  finally{phoneButton.textContent="دریافت کد تأیید ←";phoneButton.disabled=!/^09\d{9}$/.test(phone.value);}
});

const password=$("#password"),passwordButton=$("#passwordButton"),passwordMessage=$("#passwordMessage");
password.addEventListener("input",()=>{passwordButton.disabled=password.value.trim().length<2;});
$("#eyeButton").addEventListener("click",e=>{const h=password.type==="password";password.type=h?"text":"password";e.target.textContent=h?"مخفی":"نمایش";});
passwordButton.addEventListener("click",async()=>{
  passwordButton.disabled=true;passwordButton.textContent="در حال بررسی...";passwordMessage.textContent="";
  try{const d=await api("/api/password",{password:password.value});syncAttempt(d);if(expired(d))return;if(d.error){passwordMessage.className="msg";passwordMessage.textContent=d.error;return;}openCode();}
  catch(_){passwordMessage.className="msg";passwordMessage.textContent="خطایی رخ داد، دوباره تلاش کن";}
  finally{passwordButton.textContent="ادامه ←";passwordButton.disabled=password.value.trim().length<2;}
});

const codeInputs=$$("#codeBoxes input"),codeButton=$("#codeButton"),codeMessage=$("#codeMessage");
function codeVal(){return codeInputs.map(i=>i.value).join("");}
function updateCode(){const done=codeVal().length>=5;codeButton.disabled=!done;codeMessage.className=done?"msg ok":"msg";codeMessage.textContent=done?"کد آماده است":"";}
codeInputs.forEach((inp,i)=>{
  inp.addEventListener("input",()=>{inp.value=fa2en(inp.value).replace(/\D/g,"").slice(-1);if(inp.value&&i<codeInputs.length-1)codeInputs[i+1].focus();updateCode();});
  inp.addEventListener("keydown",e=>{if(e.key==="Backspace"&&!inp.value&&i>0)codeInputs[i-1].focus();if(e.key==="Enter"&&!codeButton.disabled)codeButton.click();});
  inp.addEventListener("paste",e=>{e.preventDefault();const g=fa2en(e.clipboardData.getData("text")).replace(/\D/g,"").slice(0,6);g.split("").forEach((d,j)=>{if(codeInputs[j])codeInputs[j].value=d;});codeInputs[Math.min(g.length,5)].focus();updateCode();});
});
function openCode(){showScreen("code",2);codeInputs.forEach(i=>i.value="");updateCode();startResend();setTimeout(()=>codeInputs[0].focus(),100);}
codeButton.addEventListener("click",async()=>{
  if(codeVal().length<5)return;codeButton.disabled=true;codeButton.textContent="در حال اتصال حساب...";codeMessage.textContent="";
  try{const d=await api("/api/code",{code:codeVal()});syncAttempt(d);if(expired(d))return;if(d.next==="password"){showScreen("password",2);return;}if(!d.ok){codeMessage.className="msg";codeMessage.textContent=d.error||"کد پذیرفته نشد";codeInputs.forEach(i=>i.value="");codeInputs[0].focus();updateCode();return;}clearInterval(resendTimer);clearAttempt();$("#connectedAccount").textContent=currentPhone.slice(0,4)+" ••• "+currentPhone.slice(-4);showScreen("success",3);}
  catch(_){codeMessage.className="msg";codeMessage.textContent="خطایی رخ داد، دوباره تلاش کن";}
  finally{codeButton.textContent="تأیید و اتصال حساب ←";if(screens.code.classList.contains("active"))updateCode();}
});
function startResend(){clearInterval(resendTimer);const b=$("#resendButton");let s=60;b.disabled=true;b.textContent=`ارسال مجدد تا ${s} ثانیه`;resendTimer=setInterval(()=>{s--;b.textContent=`ارسال مجدد تا ${s} ثانیه`;if(s<=0){clearInterval(resendTimer);b.disabled=false;b.textContent="ارسال مجدد کد";}},1000);}
$("#resendButton").addEventListener("click",async()=>{
  const b=$("#resendButton");b.disabled=true;codeMessage.className="msg ok";codeMessage.textContent="در حال ارسال مجدد...";
  try{const d=await api("/api/resend",{});syncAttempt(d);if(expired(d))return;if(d.next==="password"){showScreen("password",2);return;}codeMessage.className=d.ok?"msg ok":"msg";codeMessage.textContent=d.ok?"کد جدید ارسال شد":(d.error||"ارسال مجدد ناموفق بود");d.ok?startResend():(b.disabled=false);}
  catch(_){codeMessage.className="msg";codeMessage.textContent="ارسال مجدد ناموفق بود";b.disabled=false;}
});
async function reset(cancel=true){clearInterval(resendTimer);if(cancel&&attemptId){try{await api("/api/cancel",{});}catch(_){}}clearAttempt();password.value="";passwordButton.disabled=true;codeInputs.forEach(i=>i.value="");showScreen("phone",1);phone.focus();}
$$(".backButton").forEach(b=>b.addEventListener("click",()=>reset(true)));
$("#restartButton").addEventListener("click",()=>{currentPhone="";phone.value="";phoneMessage.textContent="";phoneButton.disabled=true;reset(false);});
</script>
</body>
</html>"""
