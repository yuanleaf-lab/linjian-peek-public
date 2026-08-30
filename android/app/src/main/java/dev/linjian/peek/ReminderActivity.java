package dev.linjian.peek;

import android.app.Activity;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Bundle;
import android.view.Gravity;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

/** 悬浮横幅提醒：所有 send_notification / 主动提醒统一走这里。 */
public class ReminderActivity extends Activity {
    @Override protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        SystemBars.applyEdgeToEdge(this, 0xFFFFF4F8, 0xFFFFEEF5, false);
        String title = getIntent().getStringExtra("title");
        String message = getIntent().getStringExtra("message");
        if (title == null || title.trim().isEmpty()) title = "掌心窗提醒";
        if (message == null || message.trim().isEmpty()) message = AppPrefs.userName(this) + "，看一眼这里。";

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setGravity(Gravity.CENTER_HORIZONTAL);
        int pad = dp(22);
        root.setPadding(pad, pad, pad, pad);
        root.setBackgroundResource(R.drawable.app_window_background);
        SystemBars.applyInsetPadding(root, pad, pad, pad, pad);

        TextView titleView = new TextView(this);
        titleView.setText(title);
        titleView.setTextSize(24);
        titleView.setTextColor(0xFF433039);
        titleView.setGravity(Gravity.CENTER);
        titleView.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        root.addView(titleView, new LinearLayout.LayoutParams(-1, -2));

        TextView msgView = new TextView(this);
        msgView.setText(message);
        msgView.setTextSize(17);
        msgView.setTextColor(0xFF916E7B);
        msgView.setGravity(Gravity.CENTER);
        msgView.setLineSpacing(dp(4), 1.0f);
        LinearLayout.LayoutParams mp = new LinearLayout.LayoutParams(-1, -2);
        mp.setMargins(0, dp(16), 0, dp(18));
        root.addView(msgView, mp);

        Button home = new Button(this);
        home.setText(AppPrefs.returnButtonText(this));
        home.setTextSize(16);
        home.setAllCaps(false);
        home.setTextColor(Color.WHITE);
        home.setBackground(rounded(0xFFD37091, 24, 0xFFD37091));
        home.setOnClickListener(v -> { String target = AppPrefs.homeTargetPackage(this); if (!target.isEmpty()) CompanionService.openPackageResult(this, target); finish(); });
        root.addView(home, new LinearLayout.LayoutParams(-1, dp(48)));

        Button later = new Button(this);
        later.setText("等会儿");
        later.setTextSize(15);
        later.setAllCaps(false);
        later.setTextColor(0xFFD37091);
        later.setBackground(rounded(0xFFFFFCFD, 23, 0xFFE9B8C9));
        later.setOnClickListener(v -> finish());
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(-1, dp(46));
        lp.setMargins(0, dp(10), 0, 0);
        root.addView(later, lp);

        setContentView(root);
    }

    private int dp(int v) { return Math.round(v * getResources().getDisplayMetrics().density); }
    private GradientDrawable rounded(int color, int radiusDp, int stroke) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(color);
        drawable.setCornerRadius(dp(radiusDp));
        drawable.setStroke(dp(1), stroke);
        return drawable;
    }
}
