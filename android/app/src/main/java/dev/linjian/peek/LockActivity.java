package dev.linjian.peek;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.Dialog;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.ColorDrawable;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.MotionEvent;
import android.view.Window;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.InputStream;

public class LockActivity extends Activity {
    private String pkg;
    private TextView titleView, remainView, reasonView, messageView;
    private EditText requestReasonInput;
    private final Handler handler = new Handler(Looper.getMainLooper());

    private final Runnable tick = new Runnable() { @Override public void run() { refresh(); handler.postDelayed(this, 1000); } };

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        SystemBars.applyEdgeToEdge(this, 0xFFFFF3F7, 0xFFFFEDF4, false);
        setFinishOnTouchOutside(false);
        pkg = getIntent() == null ? "" : getIntent().getStringExtra("package");
        if (pkg == null) pkg = "";
        buildUi();
        refresh();
    }

    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (intent != null && intent.getStringExtra("package") != null) pkg = intent.getStringExtra("package");
        refresh();
    }

    @Override protected void onResume() { super.onResume(); handler.removeCallbacks(tick); handler.post(tick); }
    @Override protected void onPause() { handler.removeCallbacks(tick); super.onPause(); }

    private void buildUi() {
        FrameLayout scene = new FrameLayout(this);
        scene.setBackgroundResource(R.drawable.app_gate_background);
        applyGateBackground(scene);

        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.setBackgroundColor(Color.TRANSPARENT);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setGravity(Gravity.CENTER_HORIZONTAL);
        int side = dp(26);
        int top = dp(18);
        int bottom = dp(24);
        root.setPadding(side, top, side, bottom);
        SystemBars.applyInsetPadding(root, side, top, side, bottom);
        scroll.addView(root, new ScrollView.LayoutParams(-1, -2));

        View topSpace = new View(this);
        root.addView(topSpace, lp(-1, dp(72), 0, 0, 0, 0));

        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setGravity(Gravity.CENTER_HORIZONTAL);
        card.setPadding(dp(20), dp(19), dp(20), dp(18));
        card.setBackground(rounded(0xB0FFFFFF, 28, 0x99F2D5DF, 1));
        root.addView(card, lp(-1, -2, 0, 0, 0, dp(18)));

        TextView tag = text("APP GATE  ·  应用门禁", 10, 0xFFD36F91, true);
        tag.setGravity(Gravity.CENTER_HORIZONTAL);
        tag.setLetterSpacing(.12f);
        card.addView(tag, lp(-1, -2, 0, 0, 0, 7));

        ImageView decor = new ImageView(this);
        decor.setImageResource(R.drawable.decor_gate_cat_box);
        decor.setScaleType(ImageView.ScaleType.CENTER_INSIDE);
        decor.setContentDescription(null);
        card.addView(decor, lp(dp(96), dp(74), 0, 0, 0, 6));

        titleView = text("", 22, 0xFF433039, true);
        titleView.setGravity(Gravity.CENTER_HORIZONTAL);
        card.addView(titleView, lp(-1, -2, 0, 0, 0, 10));

        remainView = text("", 12, 0xFFD37091, true);
        remainView.setGravity(Gravity.CENTER_HORIZONTAL);
        remainView.setBackground(rounded(0xB8FFE2EC, 18, 0x99F2D5DF, 1));
        remainView.setPadding(dp(15), dp(7), dp(15), dp(7));
        card.addView(remainView, lp(-2, -2, 0, 0, 0, 16));

        reasonView = card("锁定理由", "");
        card.addView(reasonView, lp(-1, -2, 0, 0, 0, 9));

        messageView = card(AppPrefs.companionName(this) + "说", "");
        card.addView(messageView, lp(-1, -2, 0, 0, 0, 13));

        requestReasonInput = new EditText(this);
        requestReasonInput.setHint("写下申请解锁的理由");
        requestReasonInput.setHintTextColor(0xFFB89AA5);
        requestReasonInput.setTextColor(0xFF433039);
        requestReasonInput.setTextSize(12);
        requestReasonInput.setSingleLine(false);
        requestReasonInput.setMinLines(2);
        requestReasonInput.setPadding(dp(15), dp(11), dp(15), dp(11));
        requestReasonInput.setBackground(rounded(0xA3FFFCFD, 20, 0x88F2D5DF, 1));
        card.addView(requestReasonInput, lp(-1, dp(70), 0, 0, 0, 26));

        LinearLayout actions = new LinearLayout(this);
        actions.setOrientation(LinearLayout.HORIZONTAL);
        actions.setGravity(Gravity.CENTER);
        actions.setWeightSum(2f);

        Button request = button("申请解锁", true);
        request.setOnClickListener(v -> {
            String reason = requestReasonInput.getText().toString().trim();
            if (reason.length() == 0) { Toast.makeText(this, "先写一句解锁理由", Toast.LENGTH_SHORT).show(); return; }
            AppGate.submitUnlockRequest(this, pkg, reason);
            Toast.makeText(this, "已把解锁申请交给" + AppPrefs.companionName(this), Toast.LENGTH_LONG).show();
        });
        LinearLayout.LayoutParams requestLp = new LinearLayout.LayoutParams(0, dp(46), 1f);
        requestLp.rightMargin = dp(5);
        actions.addView(request, requestLp);

        Button home = button("返回桌面", false);
        home.setOnClickListener(v -> goHomeAndFinishIfAllowed());
        LinearLayout.LayoutParams homeLp = new LinearLayout.LayoutParams(0, dp(46), 1f);
        homeLp.leftMargin = dp(5);
        actions.addView(home, homeLp);
        card.addView(actions, lp(-1, dp(46), 0, 0, 0, 11));

        Button emergency = textButton("长按 5 秒紧急解锁");
        final Runnable emergencyRunnable = () -> showEmergencyDialog();
        emergency.setOnTouchListener((v, event) -> {
            if (event.getAction() == MotionEvent.ACTION_DOWN) {
                handler.postDelayed(emergencyRunnable, 5000);
                Toast.makeText(this, "继续按住 5 秒，才会打开紧急解锁", Toast.LENGTH_SHORT).show();
                return true;
            }
            if (event.getAction() == MotionEvent.ACTION_UP || event.getAction() == MotionEvent.ACTION_CANCEL) {
                handler.removeCallbacks(emergencyRunnable);
                return true;
            }
            return true;
        });
        card.addView(emergency, lp(dp(168), dp(32), 0, 0, 0, 7));

        TextView foot = text("时间结束后会自动放行", 9, 0xFFB0929D, false);
        foot.setGravity(Gravity.CENTER_HORIZONTAL);
        card.addView(foot, lp(-1, -2, 0, 0, 0, 0));
        scene.addView(scroll, new FrameLayout.LayoutParams(-1, -1));
        setContentView(scene);
    }

    private void applyGateBackground(FrameLayout scene) {
        String raw = AppPrefs.get(this).getString(AppPrefs.KEY_BACKGROUND_URI, "");
        boolean imageApplied = false;
        if (raw != null && !raw.trim().isEmpty()) {
            try {
                Bitmap bitmap;
                try (InputStream input = getContentResolver().openInputStream(Uri.parse(raw))) {
                    bitmap = BitmapFactory.decodeStream(input);
                }
                if (bitmap == null) throw new IllegalStateException("bitmap_unavailable");
                ImageView image = new ImageView(this);
                image.setScaleType(ImageView.ScaleType.CENTER_CROP);
                image.setImageBitmap(bitmap);
                scene.addView(image, new FrameLayout.LayoutParams(-1, -1));
                imageApplied = true;
            } catch (Exception ignored) { }
        }
        View scrim = new View(this);
        int alpha = imageApplied ? Math.min(AppPrefs.customInt(this, AppPrefs.KEY_BACKGROUND_SCRIM, 8, 0, 90), 32) : 12;
        scrim.setBackgroundColor(Color.argb(alpha, 255, 243, 247));
        scene.addView(scrim, new FrameLayout.LayoutParams(-1, -1));
    }

    private void refresh() {
        JSONObject lock = AppGate.currentLock(this, pkg);
        if (lock == null) { finish(); return; }
        long now = System.currentTimeMillis(); long until = lock.optLong("locked_until_ms", 0); long remain = Math.max(0, until - now);
        titleView.setText(lock.optString("app_name", AppGate.labelOf(this, pkg)) + " 已被" + AppPrefs.companionName(this) + "锁定");
        remainView.setText("剩余时间：" + remainText(remain));
        String reason = lock.optString("reason", "").trim();
        String message = lock.optString("message", "").trim();
        reasonView.setText("锁定理由" + (reason.isEmpty() ? "" : "\n" + reason));
        messageView.setText(AppPrefs.companionName(this) + "说" + (message.isEmpty() ? "" : "\n" + message));
        reasonView.setVisibility(reason.isEmpty() ? View.GONE : View.VISIBLE);
        messageView.setVisibility(message.isEmpty() ? View.GONE : View.VISIBLE);
    }

    @Override public void onBackPressed() {
        if (AppGate.currentLock(this, pkg) != null) {
            Toast.makeText(this, "锁定期间不能返回受锁应用", Toast.LENGTH_SHORT).show();
            return;
        }
        super.onBackPressed();
    }

    @Override public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK && AppGate.currentLock(this, pkg) != null) {
            Toast.makeText(this, "锁定期间不能返回受锁应用", Toast.LENGTH_SHORT).show();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    private void goHomeAndFinishIfAllowed() {
        boolean enhancedHome = false;
        ScreenshotService svc = ScreenshotService.getInstance();
        if (AppPrefs.isEnhancedPrivacy(this) && svc != null) enhancedHome = svc.doHome();
        if (enhancedHome) {
            finish();
            return;
        }
        if (!enhancedHome) {
            try {
                Intent home = new Intent(Intent.ACTION_MAIN);
                home.addCategory(Intent.CATEGORY_HOME);
                home.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(home);
            } catch (Exception e) {
                DebugState.append(this, "门禁回桌面失败：" + ScreenshotService.shortMsg(e));
            }
        }
    }

    private void showEmergencyDialog() {
        Dialog dialog = new Dialog(this);
        dialog.requestWindowFeature(Window.FEATURE_NO_TITLE);
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(dp(24), dp(22), dp(24), dp(22));
        card.setBackground(rounded(0xE6FFFDFE, 26, 0x99F2D5DF, 1));
        card.addView(text("紧急解锁", 18, 0xFF433039, true), lp(-1, -2, 0, 0, 0, 10));
        TextView body = text("确认是紧急情况再用。通过后会临时放行几分钟，并写入日志。", 12, 0xFF765964, false);
        body.setLineSpacing(dp(4), 1f);
        card.addView(body, lp(-1, -2, 0, 0, 0, 16));
        EditText input = new EditText(this);
        input.setHint("输入" + AppPrefs.companionName(this) + "告诉你的紧急口令");
        input.setHintTextColor(0xFFB89AA5); input.setTextColor(0xFF433039); input.setTextSize(13);
        input.setSingleLine(true); input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        input.setEnabled(true); input.setFocusable(true); input.setFocusableInTouchMode(true);
        input.setImeOptions(android.view.inputmethod.EditorInfo.IME_ACTION_DONE);
        input.setPadding(dp(15), dp(11), dp(15), dp(11)); input.setBackground(rounded(0xE0FFFFFF, 18, 0x99F2D5DF, 1));
        card.addView(input, lp(-1, dp(48), 0, 0, 0, 18));
        LinearLayout actions = new LinearLayout(this); actions.setOrientation(LinearLayout.HORIZONTAL); actions.setWeightSum(2f);
        Button cancel = button("取消", false); Button unlock = button("解锁", true);
        actions.addView(cancel, new LinearLayout.LayoutParams(0, dp(46), 1f));
        LinearLayout.LayoutParams unlockLp = new LinearLayout.LayoutParams(0, dp(46), 1f); unlockLp.leftMargin = dp(10);
        actions.addView(unlock, unlockLp); card.addView(actions, new LinearLayout.LayoutParams(-1, dp(46)));
        cancel.setOnClickListener(v -> dialog.dismiss());
        unlock.setOnClickListener(v -> { boolean ok = AppGate.tryEmergencyUnlock(this, pkg, input.getText().toString()); Toast.makeText(this, ok ? "紧急解锁成功，临时放行" : "口令不对", Toast.LENGTH_LONG).show(); if (ok) { dialog.dismiss(); finish(); } });
        input.setOnEditorActionListener((v, actionId, event) -> { if (actionId == android.view.inputmethod.EditorInfo.IME_ACTION_DONE) { unlock.performClick(); return true; } return false; });
        dialog.setContentView(card); dialog.show();
        Window window = dialog.getWindow();
        if (window != null) { window.setBackgroundDrawable(new ColorDrawable(Color.TRANSPARENT)); window.setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_STATE_ALWAYS_VISIBLE); window.setLayout((int) (getResources().getDisplayMetrics().widthPixels * .88f), -2); }
        input.requestFocus();
    }

    private TextView text(String s, int sp, int color, boolean bold) {
        TextView t = new TextView(this); t.setText(s); t.setTextSize(sp); t.setTextColor(color); t.setIncludeFontPadding(false); t.setLineSpacing(dp(3), 1f); t.setTypeface(Typeface.create(bold ? "sans-serif-medium" : "sans-serif", Typeface.NORMAL)); return t;
    }
    private TextView card(String title, String body) { TextView t = text(title + (body.isEmpty() ? "" : "\n" + body), 12, 0xFF59414A, false); t.setPadding(dp(16), dp(13), dp(16), dp(13)); t.setBackground(rounded(0xA3FFFFFF, 22, 0x88F2D5DF, 1)); return t; }
    private Button button(String s, boolean primary) { Button b = new Button(this); b.setText(s); b.setAllCaps(false); b.setTextSize(11); b.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL)); b.setTextColor(primary ? Color.WHITE : 0xFFD37091); b.setMinWidth(0); b.setMinHeight(0); b.setPadding(dp(10), 0, dp(10), 0); b.setBackground(rounded(primary ? 0xFFD37091 : 0xFFFFFCFD, 20, primary ? 0xFFD37091 : 0xFFE9B8C9, 1)); return b; }
    private Button textButton(String s) { Button b = new Button(this); b.setText(s); b.setAllCaps(false); b.setTextSize(9); b.setTextColor(0xFFAD7F90); b.setMinHeight(0); b.setPadding(dp(8), 0, dp(8), 0); b.setBackground(rounded(0x00FFFFFF, 16, 0x00FFFFFF, 0)); return b; }
    private GradientDrawable rounded(int color, int radius, int stroke, int strokeWidth) { GradientDrawable g = new GradientDrawable(); g.setColor(color); g.setCornerRadius(dp(radius)); if (strokeWidth > 0) g.setStroke(strokeWidth, stroke); return g; }
    private LinearLayout.LayoutParams lp(int w, int h, int l, int t, int r, int b) { LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(w, h); p.setMargins(l,t,r,b); return p; }
    private int dp(int v) { return (int)(v * getResources().getDisplayMetrics().density + 0.5f); }
    private String remainText(long ms) {
        long sec = ms / 1000; long h = sec / 3600; long m = (sec % 3600) / 60; long s = sec % 60;
        if (h > 0) return h + " 小时 " + m + " 分钟 " + s + " 秒";
        if (m > 0) return m + " 分钟 " + s + " 秒";
        return s + " 秒";
    }
}
