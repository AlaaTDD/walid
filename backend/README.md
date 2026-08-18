# Sheet Nesting Backend — Optimized Build

Backend لـFlutter/print-nesting workflow: تحليل RGBA، استخراج contour، irregular nesting، collision validation، compositing، TIFF export، وQA.

## أهم التغييرات

- تحليل الصورة مرة واحدة أثناء الرفع بدل إعادة فتح وتحليل الـalpha أكثر من مرة.
- تخزين `source_centroid_px` و`alpha_bbox_px` لتسريع الـcompositor ومنع عدم التطابق بين centroid التحليل والـraster.
- قصّ الشفافية الزائدة قبل اللصق، مما يقلل كمية الـpixels التي تتم معالجتها.
- منع اختلاف الـDPI بين `/upload` و`/layout/compute`.
- ترتيب الشيت يتوقف مبكرًا عندما لا يبقى أي شكل من الأشكال المتبقية له موضع هندسي صالح.
- الرد النهائي يوضح عدد القطع التي دخلت، وعدد المرتب، وعدد المتبقي، وهل الشيت وصل للسعة الهندسية.
- دمج مناطق الـclearance قبل حساب الـNFP بدل حساب/دمج منطقة لكل قطعة في كل زاوية.
- إعادة استخدام triangulation لمنطقة الشيت المشغولة بين زوايا الدوران.
- `STRtree` لتقليل فحوصات collision/QA البعيدة مع بقاء القرار النهائي GEOS exact.
- إعادة فحص collision قبل التصدير، ثم QA مستقل بعد التصدير.
- لون الخلفية قابل للضبط من الإعدادات (`white`، `black`، `gray`، أو `#RRGGBB`) ويحفظ كـBackground layer مستقل.
- TIFF قابل للتحرير في Photoshop/Affinity/Krita: كل صورة مرتبة تُحفظ Layer مستقلة بلا إعادة تحجيم أو flatten لبيانات الطبقات؛ وتبقى معاينة TIFF المدمجة للتوافق مع برامج TIFF العادية.
- الـpacking الآن يجرب حتى خمس ترتيبات exact مختلفة للصور ذات المقاسات المختلفة ويختار الصفحة ذات السعة الأعلى، من دون تعديل المقاسات أو الـDPI أو الـclearance.
- بعد QA الناجح فقط يمكن نقل الصور الأصلية التي تم وضعها فعلاً إلى مسار أرشيف تختاره من الإعدادات، داخل مجلد بتاريخ ووقت العملية. يتم التحقق من SHA-256 قبل النقل وتبقى الصور غير المعالَجة في مكانها.
- حماية `job_id` ضد path traversal، وatomic job-state writes.
- سقف لحجم الرفع وعدد pixels لحماية السيرفر من الملفات الضخمة أو decompression bombs.
- إصلاح اتساق دوران 90°/270° بين هندسة Shapely وإحداثيات الصور.
- حذف `.venv` و`__pycache__` و`.DS_Store` من الحزمة النهائية.

## API note

`POST /upload` يدعم حاليًا `dpi` كـmultipart form field (الافتراضي 300). استخدم نفس القيمة في `POST /layout/compute/{job_id}`.

عند امتلاء الشيت، `ComputeResponse.sheet_full=true` وتجد رسالة جاهزة في `layout_message` مع `unplaced_part_ids`.

## إعدادات التصدير الجديدة

- `packing_attempts`: ثابت دائمًا على 5 (الأقصى، وهو الوحيد المعروض من واجهة Flutter). عدد محاولات ترتيب exact مستقلة؛ يزيد زمن الحساب ولكنه لا يغير أي قيد للطباعة.
- `background_color`: لون Pillow/CSS معتم مثل `#FFFFFF` أو `black`.
- `processed_images_path`: مسار اختياري على نفس جهاز الـbackend. عند نجاح TIFF وQA، تُنقل فقط الملفات التي دخلت الـTIFF إلى مجلد مثل `2026-08-13_20-30-15/` تحته. يرسل عميل Flutter مسارات الملفات المنتقاة تلقائيًا في تطبيقات سطح المكتب.

## تشغيل

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## الاختبارات

```bash
PYTHONPATH=. pytest -q
```
