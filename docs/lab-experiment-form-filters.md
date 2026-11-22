# فلاتر نموذج Run New Experiment — ملخص تعريفي

مرجع لحقول نموذج **Run New Experiment** في لوحة المختبر (`laboratory/dashboard.py` → `/experiments/run`).  
القيم تُمرَّر إلى `ExperimentConfig` ثم إلى `score_model` عبر walk-forward (`lab-run`).  
**لا Gemini** في مسار التجارب؛ التسعير إحصائي فقط.

## 0) JSON config (اختياري — يتجاوز النموذج)

صندوق `config_json` في أعلى النموذج يقبل كائن JSON كاملاً. إن لم يكن فارغاً **يتجاهل** حقول النموذج الأخرى.

- التحليل: `laboratory.lab_experiment.parse_experiment_launch_json`
- المفاتيح الناقصة تأخذ الافتراضيات (`DEFAULT_BASE_CONFIG` + `primary_metric=log_loss` + `min_matches=50` + `competition=PL`)
- **مطلوب:** `seasons` (مثل `[2024, 2025]`)
- الأخطاء (JSON غير صالح، دوري غير معروف، `min_matches < 10`، …) تُعاد كـ `run_status=failed` دون إنشاء تجربة

شكل مكافئ لـ YAML:

```json
{
  "experiment_id": "EXP-CUSTOM-001",
  "competitions": ["PL"],
  "seasons": [2024, 2025],
  "primary_metric": "log_loss",
  "min_matches": 50,
  "base_config": {
    "k": 6.0,
    "prior_mean": "last_season",
    "dependence": "independent",
    "half_time_share_mode": "fixed_0_45",
    "ratio_mode": "one_pass",
    "draw_boost": 1.0
  },
  "param_grid": {
    "k": [5.0, 6.0, 7.0],
    "draw_boost": [1.0, 1.1, 1.2]
  }
}
```

اختصارات مسطّحة مدعومة أيضاً: `k`, `grid_k`, `draw_boost`, `grid_draw_boost`, …

---

## 1) اختيار البيانات

### Competitions (المسابقات)
أي دوريات تُشغَّل عليها التجربة. كل دوري مختار يصبح تجربة منفصلة؛ اختيار أكثر من دوري ينشئ **batch** منسّق (`BATCH-…`).  
الأكواد المعتمدة في الواجهة: `PL`, `PD`, `FL1`, `BL1`, `SA`, `PPL`, `DED`, `CL`, `SPL`.

### Seasons (المواسم)
سنوات الموسم المستخدمة في walk-forward. إن وُجدت قائمة مواسم فقط (بدون tuning/holdout صريح)، يقسمها المختبر تقريباً **80% tuning / 20% holdout** حسب ترتيب السنوات (`ExperimentConfig`).

---

## 2) إعدادات التجربة العامة

### Experiment ID (optional slug)
معرّف اختياري للتجربة (أحرف/أرقام/`_`/`-` فقط).  
- تجربة واحدة: يُستخدم كما هو إن وُجد، وإلا يُولَّد مثل `EXP-{COMP}-{timestamp}-{hash}`.  
- عدة مسابقات: يُلحق كود الدوري (`{slug}-{COMP}`).

### Primary Metric (المقياس الأساسي)
معيار ترتيب شبكة المعاملات (leaderboard / اختيار الأفضل):

| قيمة الواجهة | المفتاح الداخلي | الاتجاه |
| --- | --- | --- |
| Log Loss | `full_time_1x2_log_loss` | أقل أفضل |
| Brier Score | `full_time_1x2_brier` | أقل أفضل |
| FT 1X2 Accuracy | `full_time_1x2_accuracy` | أعلى أفضل |
| Exact Score Log-Loss | `exact_score_log_loss` | أقل أفضل |

Log loss / Brier يقيسان **جودة الاحتمالات** على نتيجة 1X2 الفعلية؛ الدقة تقيس صحة اختيار `most_likely` فقط.  
Exact Score Log-Loss يقيس جودة احتمال **خلية النتيجة الدقيقة الفعلية** (ليس خلية الـ mode) عبر `evaluate._exact`.  
الترتيب في `lab_runner` ولوحة التجربة يستخدمان المفتاح الداخلي عبر `resolve_primary_metric` (مثلاً `log_loss` → `full_time_1x2_log_loss`).

في بطاقات القائمة وجدول الـ Leaderboard وبرومبت التشخيص تُعرض أيضاً:
- **Exact Hit %** / **Exact LL** من `exact_score_*` (أو الكتلة المتداخلة)
- **O/U 2.5 Acc** / **O/U 2.5 LL** من `over_under_2_5_*`

التجارب القديمة بدون هذه المفاتيح تُعرض كـ `—` دون كسر الصفحة.

### Min Matches
الحد الأدنى لعدد المباريات المقيَّمة (`matches_evaluated`) قبل تتويج **Best config** في `lab_runner.pick_best_run` (افتراضي الواجهة: `50`).  
التشغيلات الأصغر تبقى ظاهرة في لوحة النتائج مع شارة `n<min_matches`، لكنها **لا** تُختار كأفضل إعداد.

---

## 3) إعدادات النموذج الأساسية (`base_config`)

### Base k (shrinkage)
قوة انكماش نسب الهجوم/الدفاع نحو المتوسط:

\[
r_{\mathrm{shrunk}} = \frac{n\cdot r + k\cdot \mathrm{prior}}{n + k}
\]

- `k` أكبر → ثقة أقل في العيّنة القصيرة ورجوع أقوى للـ prior.  
- الافتراضي الحي: `6.0` (`score_model.K`).  
- إن مُلئت **k Grid Search** تتجاهل قيمة Base لكل تركيبة شبكة (تبقى في `base_config` كمرجع فقط).

### Prior Mean
ماذا يكون متوسط الـ prior قبل انكماش نسب الأندية:

| القيمة | المعنى |
| --- | --- |
| `one` | prior = `1.0` (محايد) |
| `last_season` | prior من نسب الموسم السابق المنكمشة لناديّي المباراة (عند توفرها) |

`last_season` هو الافتراضي في الواجهة وفي `DEFAULT_LIVE_STACK`.

### Dependence
هل أهداف الفريقين مستقلة بعد تقدير λ:

| القيمة | المعنى |
| --- | --- |
| `independent` | شبكة بواسون مستقلة (الأساس) |
| `dixon_coles` | تصحيح خلايا النتائج المنخفضة (0-0 / 1-0 / 0-1 / 1-1) بعامل ρ يُقدَّر من مباريات **أسبق** فقط |

### Half-Time Share
كيف تُشتقّ حصّة الشوط الأول من λ الكاملة لسوق HT:

| القيمة | المعنى |
| --- | --- |
| `fixed_0_45` | حصّة ثابتة 0.45 لكل طرف (افتراضي الواجهة) |
| `club_shrunk` | حصّة منكمشة لكل نادٍ من بياناته |
| `league_only` | حصّة دوري فقط بدون تخصّص نادٍ |

إذا لم تكفِ عيّنات HT (≥ 3) يُحجب سوق الشوط الأول (`withheld`) ولا يُملأ بأصفار.

### Ratio Mode
طريقة حساب نسب الهجوم/الدفاع من المباريات السابقة:

| القيمة | المعنى |
| --- | --- |
| `one_pass` | تمريرة واحدة؛ قد تبقى انحيازات الجدول (schedule bias) |
| `joint` | حلّ هجوم/دفاع بشكل مشترك لتقليل انحياز الجدول |

---

## 4) شبكة البحث (Grid Search)

تُوسَّع كمنتج ديكارتي مع بقية معاملات الشبكة (`expand_param_grid`). كل تركيبة = تشغيل walk-forward بهوية `model_id` حتمية (SHA-256).

### k Grid Search
قائمة `k` مفصولة بفواصل (مثل `5.0, 6.0, 7.0`).  
إن تُركت فارغة يُستخدم **Base k** كقيمة وحيدة في الشبكة.

### Draw Boost Grid
قائمة `draw_boost` مفصولة بفواصل (مثل `1.0, 1.1, 1.2`).  
إن تُركت فارغة يُستخدم حقل **Draw Boost** المتقدم كقيمة وحيدة.

> حجم الشبكة ≈ (عدد قيم k) × (عدد قيم draw_boost) × (أي أبعاد متقدمة أُدخلت كقوائم). اختبر شبكات صغيرة أولاً.

---

## 5) معايرة التعادل المتقدمة (Advanced Calibration)

القيم المفردة هنا تُحفظ في **`base_config`** وتُطبَّق على كل تركيبة شبكة. شبكة البحث في الواجهة تبقى لـ `k` و `draw_boost` فقط.

### Draw Boost
مضاعف كتلة خلايا **التعادل** على شبكة النتيجة (القطر: 0-0، 1-1، …) قبل إعادة التطبيع.  
- `1.0` = بدون تعزيز.  
- `> 1` يزيد احتمال التعادلات في التوزيع → يؤثر على log-loss / Brier / النتيجة الدقيقة / Over-Under.  
**ليس** مجرد تغيير لملصق الاختيار.  
إن مُلئت **Draw Boost Grid** تستبدل الشبكة هذه القيمة لكل تركيبة.

### Draw Weights (comma-separated)
أوزان موجبة لكل مستوى تعادل على القطر (مثال شائع: `1.35, 1.25, 1.05, 0.70` لـ 0-0 ثم 1-1 ثم 2-2 ثم 3-3…).  
توزّع تعزيز التعادل بشكل غير منتظم (غالباً أقوى على النتائج المنخفضة). تُترك فارغة = وزن موحّد تحت `draw_boost`.

### Draw Pick Threshold
عتبة اختيار التعادل في سوق 1X2 عبر `select_1x2_outcome`.  
تغيّر **`most_likely` فقط**، **لا** تعيد توزيع الاحتمالات المستخدمة في log-loss/Brier.  
مثال مروَّج لـ SPL: `0.28`.

### Draw Pick Margin
هامش إضافي مع العتبة قبل اعتماد اختيار «تعادل». أيضاً يؤثر على التسمية فقط لا على المتجه الاحتمالي. مثال مروَّج لـ SPL: `0.05`.

### Draw Tightness Scale
يُخفّف أثر `draw_boost` عندما تكون المباراة «مفتوحة» (فرق λ كبير) ويقوّيه عندما تكون متقاربة (tight).  
`None` / فارغ = بدون تعديل حسب التقارب. القيمة الفعّالة تُبلَّغ كـ `effective_draw_boost`.

### Draw Tightness Ref
مرجع مقياس التقارب (افتراضي `1.0`) المستخدم مع `draw_tightness_scale`.

---

## تذكير سريع: تعزيز التوزيع مقابل قواعد الاختيار

| المجموعة | الحقول | ماذا تغيّر؟ |
| --- | --- | --- |
| معايرة كتلة التعادل | `draw_boost`, `draw_weights`, `draw_tightness_*` | مصفوفة الاحتمالات → metrics الاحتمالية |
| قواعد الاختيار | `draw_pick_threshold`, `draw_pick_margin` | اختيار `most_likely` / الدقة الظاهرة فقط |

لا تخلط بينهما عند تفسير النتائج أو عند الترقية إلى `configs/live/{code}_stack.json`.

---

## مصادر الكود

- واجهة النموذج: `laboratory/dashboard.py` (`_run_experiment_form`)
- تحليل POST: `laboratory/__main__.py` (`/experiments/run`)
- تعريف التجربة: `laboratory/lab_experiment.py` (`ExperimentConfig`, `primary_metric_key`)
- الرياضيات: `score_model.py` (`estimate_score`, `_boost_draw_scorelines`, `select_1x2_outcome`)
- المكدس الحي الافتراضي: `league_stacks.DEFAULT_LIVE_STACK`
- مثال YAML: `experiments/draw_boost_test.yaml`
