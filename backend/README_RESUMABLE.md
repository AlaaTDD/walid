# Sheet Nesting — Resumable Release

هذه النسخة تحتوي على Backend + Flutter Frontend متكاملين مع بروتوكول رفع قابل للاستئناف.

## ماذا يحدث لو انقطع التنفيذ عند الصورة 60 من 100؟

1. التطبيق ينشئ `job` قبل أول upload ويحفظ `job_id` محليًا.
2. الصور تُرسل في دفعات صغيرة، وليس في request واحد ضخم.
3. الـbackend يحفظ كل صورة بعد نجاح تحليلها مباشرة داخل حالة الـjob.
4. كل صورة لها `client_part_id` ثابت من الفرونت.
5. عند انقطاع الاتصال أو ضياع response، إعادة إرسال نفس الصورة لا تنشئ نسخة ثانية؛ الـbackend يتعرف عليها ويعيد النتيجة الموجودة.
6. عند إعادة تشغيل التطبيق، الفرونت يستعيد manifest المحلي والـjob من السيرفر ويعيد رفع `pending` فقط.
7. الملفات تُحفظ محليًا في Application Support على المنصات الأصلية حتى لا تعتمد عملية الاستئناف على بقاء ملف المستخدم الأصلي في نفس مكانه.

### مثال

لو كان لديك 100 صورة وفشل الاتصال بعد حفظ وتحليل الصورة 60:

- Server state: 60 صورة محفوظة.
- Local manifest: نفس الـjob + الـ100 معرفات.
- Restart: السيرفر يرجع أن 60 موجودة.
- Frontend: يرسل 61–100 فقط.
- لو حصل انقطاع أثناء دفعة 6 صور، يعيد الدفعة نفسها بأمان؛ الصور التي وصلت بالفعل يتم deduplicate لها بدل إنشاء نسخ جديدة.

## API الجديد

- `POST /jobs` إنشاء job ثابت قبل الرفع.
- `GET /jobs/{job_id}` استعادة حالة job.
- `POST /upload` رفع idempotent باستخدام `client_part_ids_json`.
- `DELETE /jobs/{job_id}/parts/{client_part_id}` حذف صورة من job ومحو الـlayout القديم.
- `DELETE /jobs/{job_id}` حذف job بالكامل.
- `POST /layout/compute/{job_id}` الحساب، مع إعادة استخدام نتيجة محفوظة إذا كانت إعدادات الحساب مطابقة.
- `POST /layout/confirm/{job_id}` التصدير، وهو idempotent أيضًا إذا كان TIFF النهائي موجودًا.
- `GET /download/{job_id}` تنزيل TIFF.

## التخزين

الـbackend لم يعد يعتمد افتراضيًا على `/tmp/nesting_jobs`؛ المسار الافتراضي أصبح:

`~/.sheet_nesting_jobs`

وللتحكم فيه استخدم:

`NESTING_JOBS_ROOT=/path/to/persistent/jobs`

هذا مهم لأن `/tmp` مناسب للملفات المؤقتة، وليس أفضل اختيار للاستئناف بعد إعادة تشغيل الجهاز.

## Flutter

تمت إضافة `path_provider` واستخدام Job Persistence محلي. في macOS/Windows/Linux/iOS/Android يتم حفظ ملفات الـjob في Application Support. الويب يستمر داخل الجلسة الحالية، بينما الاستئناف الكامل بعد إغلاق المتصفح يحتاج طبقة browser storage مخصصة للملفات الكبيرة.

## الاختبارات

Backend: جميع اختبارات الـAPI والهندسة والـresume موجودة داخل `backend/tests`.
