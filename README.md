# نظام الحضور والانصراف — Attendance & Departure System

نظام ثنائي اللغة (عربي RTL / English LTR) لتسجيل الحضور والانصراف بالموقع الجغرافي، يتكون من:

- **تطبيق جوال للموظف (PWA):** يُثبَّت من المتصفح على الشاشة الرئيسية. يسجل الحضور والانصراف مع GPS، ويعمل دون اتصال ويزامن العمليات لاحقًا.
- **لوحة إدارة ويب متجاوبة:** للموظفين والورديات والمواقع والإجازات والتقارير والمستخدمين والإعدادات وسجل التدقيق.
- **خادم وقاعدة بيانات مركزية:** Python 3.10+ و SQLite. يعتمد على المكتبة القياسية فقط، فلا يحتاج إلى تثبيت أي حزم.

A bilingual (Arabic/English) GPS attendance system: an installable mobile PWA for employees, a responsive admin dashboard, and a central Python + SQLite backend with **zero dependencies**.

---

## 1. التشغيل · Running

```bat
run.bat                    (أو / or)   py server.py
```

- افتح `http://localhost:8000` على الكمبيوتر.
- عند أول تشغيل تُنشأ بيانات تجريبية: 12 موظفًا و3 ورديات (منها وردية ليلية تمتد لليوم التالي) وموقعان وسجلات حضور لآخر 5 أسابيع.

| الدور / Role | المستخدم / User | كلمة المرور / Password |
|---|---|---|
| مدير النظام / Admin | `admin` | `Admin@123` |
| الموارد البشرية / HR | `hr` | `Hr@12345` |
| مدير مباشر / Line manager (فريق تقنية المعلومات) | `manager` | `Manager@123` |
| موظف / Employee | `1002` … `1012` | `Emp@1234` |

خيارات / Options:

| الأمر | الوظيفة |
|---|---|
| `py server.py --port 8443` | منفذ مختلف / different port |
| `py server.py --reset-demo` | حذف القاعدة وإعادة إنشاء البيانات التجريبية / wipe & reseed |
| `py server.py --no-demo` | بدء نظيف بدون بيانات تجريبية (يطبع كلمة مرور admin) / clean start |
| `ATT_ADMIN_PASSWORD=...` | تحديد كلمة مرور admin الأولى مع `--no-demo` |
| `ATT_DATA_DIR=...` | مجلد البيانات (الافتراضي `data/`) |

### الجوال و HTTPS · Phones & HTTPS

المتصفحات لا تسمح بتحديد الموقع (GPS) إلا عبر **HTTPS** أو `localhost`. لتجربة النظام من جوال على نفس الشبكة:

1. شغّل `gen_cert.bat` لإنشاء شهادة ذاتية التوقيع في `certs/`.
2. أعد تشغيل الخادم، فيعمل تلقائيًا على `https://<IP>:8000`.
3. افتح الرابط من الجوال، واقبل تحذير الشهادة، ثم اختر **«إضافة إلى الشاشة الرئيسية»**.

> **للإنتاج:** ضع الخادم خلف وكيل عكسي بشهادة حقيقية (Caddy أو nginx أو IIS أو Cloudflare Tunnel). الشهادة الحقيقية مطلوبة أيضًا ليعمل الـ Service Worker، أي التطبيق دون اتصال، بشكل كامل على الجوال.

Browsers only expose GPS over HTTPS. Run `gen_cert.bat` for a self-signed LAN certificate; in production put the server behind a reverse proxy with a real certificate (also required for full offline/service-worker support on phones).

### على الإنترنت لعدة فروع · Online for several sites (Cloudflare Tunnel)

يبقى الخادم على هذا الجهاز، وتوفر Cloudflare رابط HTTPS حقيقيًا يعمل من أي فرع دون إعداد الراوتر.

1. شغّل الخادم بوضع الوكيل: `py server.py --behind-proxy` (أو `autostart.ps1 -BehindProxy` للتشغيل التلقائي). في هذا الوضع يستمع الخادم على `127.0.0.1` فقط، ويقرأ عنوان IP الحقيقي للمستخدم من Cloudflare.
2. **تجربة سريعة:** `powershell -ExecutionPolicy Bypass -File online.ps1` يعطي رابطًا مؤقتًا `https://….trycloudflare.com` يتغير مع كل تشغيل.
3. **رابط دائم** (مثل `https://attendance.example.com`): أضف نطاقك إلى حساب Cloudflare مجاني، ثم أنشئ Tunnel من Zero Trust ← Networks ← Tunnels، وأضف Public Hostname يشير إلى `HTTP localhost:8000`. بعدها شغّل في PowerShell كمسؤول: `online.ps1 -Token <token>`.

> **قبل النشر:** غيّر كلمات مرور الحسابات التجريبية أو ابدأ بـ `--no-demo`، لأن الرابط سيكون متاحًا للجميع. ويجب أن يبقى الجهاز يعمل ومتصلًا.

Run `py server.py --behind-proxy`, then `online.ps1` (temporary URL) or `online.ps1 -Token <token>` (permanent hostname on your Cloudflare domain, installed as a Windows service). **Change the demo passwords before going public.**

---

## 2. إدارة المستخدمين والصلاحيات · Users & roles

| الدور | يرى | يستطيع |
|---|---|---|
| **مدير النظام** admin | كل شيء | كل الصلاحيات + الإعدادات + النسخ الاحتياطي + إنشاء مدراء النظام |
| **الموارد البشرية** hr | كل الموظفين | الموظفون، الورديات، المواقع، الأقسام، الإجازات، تعديل سجلات الحضور، التقارير، مستخدمو (مدير مباشر/موظف) |
| **مدير مباشر** manager | فريقه فقط (من «المدير المباشر» = هو) | التقارير ولوحة المتابعة وخرائط GPS لفريقه، وتسجيل إجازات/مهام لفريقه |
| **موظف** employee | نفسه فقط | تسجيل الحضور/الانصراف وسجله الشخصي |

خطوات إضافة موظف جديد / Adding an employee:
1. **الموظفون ← إضافة:** الرقم الوظيفي والاسم بالعربية والإنجليزية والقسم والوردية وموقع العمل والمدير المباشر، وفعّل «إلزام التحضير من داخل النطاق» عند الحاجة. لموظف يعمل في أكثر من فرع، اختر **«مواقع عمل إضافية»**؛ فيُقبل التحضير من داخل نطاق أي موقع منها، ويُحفظ مع كل عملية الموقع الذي سُجلت منه. *(Additional work locations: a punch is accepted inside any of them, and the matched site is stored.)*
2. **المستخدمون ← إضافة:** أنشئ اسم مستخدم وكلمة مرور (8 أحرف على الأقل، حرف ورقم) بدور «موظف»، واربطه بالموظف.
3. يفتح الموظف التطبيق على جواله ويسجل الدخول.

---

## 3. التحضير عبر الجوال · Mobile punching

- زرّا **تسجيل الحضور** و**تسجيل الانصراف** يُفعَّلان حسب حالة اليوم، ويمنع النظام التكرار في الواجهة وفي الخادم.
- **لا يُقرأ الموقع إلا لحظة الضغط على الزر.** تظهر للموظف حالة الموقع (جارٍ التحديد / تم / تعذر مع السبب) والدقة والإحداثيات والعنوان التقريبي والمسافة عن موقع العمل، مع خريطة.
- إذا كان الموظف ملزمًا بالنطاق الجغرافي، تُرفض العملية خارج النطاق أو مع دقة ضعيفة، ويُعرض السبب. وتُسجل المحاولة المرفوضة في التنبيهات وسجل التدقيق.
- **دون اتصال:** تُحفظ العملية على الجهاز مع وقتها الفعلي، وتُرسل تلقائيًا عند عودة الاتصال. في السجلات يظهر «وقت التسجيل الفعلي» و«وقت المزامنة» منفصلين. يرفض الخادم الأوقات المستقبلية والعمليات الأقدم من الحد المحدد (72 ساعة افتراضيًا).
- **الورديات الليلية:** مثلًا حضور 22:00 وانصراف 06:00. تُنسب العمليتان إلى يوم بداية الوردية.

---

## 4. التقارير · Reports

- **التقرير اليومي حسب الوردية:** المجدولون لكل وردية، ووقت الحضور والانصراف الفعلي، والحالة (حاضر، متأخر، انصرف مبكرًا، غائب، إجازة، مهمة رسمية، مستثنى، لم يسجل الانصراف)، ومدة التأخير والعمل والإضافي، وقسم مستقل للمتغيبين، وزر خريطة لموقعي الحضور والانصراف.
- **التقرير الشهري:** ملخص لكل موظف، وإجماليات حسب القسم والوردية، ونسبة الحضور، ورسم بياني. يمكن التصفية بالفترة والقسم والوردية والموظف.
- **التصدير:** زر «تصدير Excel» يُنتج ملف `.xlsx` بلغة الواجهة الحالية (مع اتجاه RTL للعربية). زر «طباعة / PDF» يفتح الطباعة بتنسيق A4 أفقي، ثم اختر «حفظ كـ PDF».

---

## 5. الأمان والتدقيق · Security & audit

- كلمات المرور مُجزّأة بـ PBKDF2-SHA256 (150 ألف تكرار). يُقفل الحساب 10 دقائق بعد 5 محاولات فاشلة، مع حد للمحاولات لكل IP.
- الجلسات في ملفات تعريف `HttpOnly` و`SameSite`، و`Secure` عند HTTPS. هناك حماية من CSRF عبر ترويسة مخصصة، وترويسات أمان (CSP و X-Frame-Options وغيرها).
- يُفحص كل طلب حسب الدور ونطاق البيانات من جهة الخادم.
- أي إضافة أو تعديل أو حذف لسجل حضور يتطلب **سببًا إلزاميًا**، ويُحفظ في **سجل التدقيق** مع القيمة قبل التعديل وبعده واسم المستخدم والوقت وعنوان IP. يُسجل أيضًا الدخول والمحاولات الفاشلة والتصدير والنسخ الاحتياطي.
- **النسخ الاحتياطي:** نسخة تلقائية يومية في `data/backups/` (يُحتفظ بآخر 14 نسخة، والعدد قابل للتعديل)، مع زر نسخ وتنزيل فوري في الإعدادات. للاستعادة: أوقف الخادم وانسخ النسخة إلى `data/attendance.db`.
- سجل الخادم في `data/server.log`.

---

## 6. الافتراضات · Assumptions (please confirm)

1. **منصة الجوال PWA** وليست تطبيقًا أصليًا من المتاجر. تعمل على Android و iOS، وتُثبت من المتصفح وتدعم GPS والعمل دون اتصال. *If store apps are required, the same REST API can back a Flutter/React Native client.*
2. **المنطقة الزمنية** للمنشأة UTC+3 (الرياض) افتراضيًا، وتُعدل من الإعدادات. الأوقات تُحسب وتُعرض بتوقيت المنشأة وليس بتوقيت الجهاز.
3. **أيام العمل** الافتراضية في البيانات التجريبية من الأحد إلى الخميس، وتُضبط لكل وردية.
4. **الوردية الحالية للموظف** هي المستخدمة في احتساب التقارير، فلا يُحفظ تاريخ تغيير الورديات. ويُستبعد الموظف «غير النشط» من التقارير.
5. **احتساب التأخير** يبدأ من بداية الوردية متى تجاوز فترة السماح. **الإضافي** هو ما بعد نهاية الوردية متى تجاوز الحد المحدد، والعمل في يوم الراحة كله إضافي. **الاستراحة** تُخصم من مدة العمل.
6. **الخرائط** من OpenStreetMap و**العنوان التقريبي** من Nominatim، وكلاهما مجاني ويعمل عند توفر الإنترنت فقط. للاستخدام الكثيف يُنصح بمزود مدفوع (Google Maps أو Mapbox).
7. **PDF** يُنتج عبر طباعة المتصفح، لضمان عرض النص العربي بشكل صحيح.
8. SQLite مناسب لمئات الموظفين. للمنشآت الكبيرة أو للتشغيل على أكثر من خادم يُنصح بالانتقال إلى PostgreSQL.

---

## 7. البنية · Structure

```
server.py              REST API + static server + SQLite + reports + XLSX writer + backups
static/index.html      app shell
static/js/app.js       UI (admin dashboard + mobile punch screen), offline queue
static/js/i18n.js      Arabic / English strings
static/css/app.css     responsive RTL/LTR styles + print (PDF) styles
static/sw.js           service worker (offline app shell)
tools/make_icons.py    regenerates PNG icons
data/                  database, backups, server.log (created at runtime)
```

### API (مختصر)

| Method | Path | Role |
|---|---|---|
| POST | `/api/login`, `/api/logout` | public / any |
| GET | `/api/me` · PUT `/api/me/lang` · PUT `/api/me/password` | any |
| POST | `/api/punch` `{uuid,type,lat,lng,accuracy,client_time,offline}` | any (linked employee) |
| GET | `/api/my/punches` | any |
| GET/POST/PUT/DELETE | `/api/{employees,shifts,locations,departments,leaves}` | staff (scoped) |
| GET/POST/PUT/DELETE | `/api/punches` (edits require `reason`) | admin, hr |
| GET | `/api/dashboard`, `/api/reports/daily`, `/api/reports/monthly` (+ `.xlsx?lang=ar|en`) | staff |
| GET/POST/PUT/DELETE | `/api/users` | admin, hr |
| GET/PUT | `/api/settings` · `/api/backups` · `/api/audit` | admin / admin, hr |
