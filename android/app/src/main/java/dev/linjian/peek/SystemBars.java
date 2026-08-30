package dev.linjian.peek;

import android.app.Activity;
import android.graphics.Color;
import android.graphics.drawable.ColorDrawable;
import android.os.Build;
import android.view.View;
import android.view.Window;
import android.view.WindowInsets;

final class SystemBars {
    private SystemBars() { }

    static void applyEdgeToEdge(Activity activity, int statusFallback, int navigationFallback, boolean dark) {
        Window window = activity.getWindow();
        window.setBackgroundDrawable(new ColorDrawable(navigationFallback));
        if (Build.VERSION.SDK_INT >= 30) {
            window.setDecorFitsSystemWindows(false);
        }
        if (Build.VERSION.SDK_INT >= 29) {
            window.setStatusBarColor(Color.TRANSPARENT);
            window.setNavigationBarColor(Color.TRANSPARENT);
            window.setStatusBarContrastEnforced(false);
            window.setNavigationBarContrastEnforced(false);
        } else {
            window.setStatusBarColor(statusFallback);
            window.setNavigationBarColor(navigationFallback);
        }
        applyLightBarFlags(window.getDecorView(), dark);
    }

    static void applyContainedBars(Activity activity, int statusColor, int navigationColor, boolean dark) {
        Window window = activity.getWindow();
        window.setBackgroundDrawable(new ColorDrawable(navigationColor));
        window.setStatusBarColor(statusColor);
        window.setNavigationBarColor(navigationColor);
        if (Build.VERSION.SDK_INT >= 29) window.setNavigationBarContrastEnforced(false);
        applyLightBarFlags(window.getDecorView(), dark);
    }

    static void applyInsetPadding(View view, int left, int top, int right, int bottom) {
        if (view == null || Build.VERSION.SDK_INT < 20) return;
        view.setOnApplyWindowInsetsListener((target, insets) -> {
            int insetLeft = insets.getSystemWindowInsetLeft();
            int insetTop = insets.getSystemWindowInsetTop();
            int insetRight = insets.getSystemWindowInsetRight();
            int insetBottom = insets.getSystemWindowInsetBottom();
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets systemBars = insets.getInsets(WindowInsets.Type.systemBars());
                insetLeft = systemBars.left;
                insetTop = systemBars.top;
                insetRight = systemBars.right;
                insetBottom = systemBars.bottom;
            }
            target.setPadding(
                    left + insetLeft,
                    top + insetTop,
                    right + insetRight,
                    bottom + insetBottom);
            return insets;
        });
        view.requestApplyInsets();
    }

    static void applyStatusBarTopPadding(View view, int left, int top, int right, int bottom) {
        if (view == null || Build.VERSION.SDK_INT < 20) return;
        view.setOnApplyWindowInsetsListener((target, insets) -> {
            int insetTop = insets.getSystemWindowInsetTop();
            if (Build.VERSION.SDK_INT >= 30) {
                insetTop = insets.getInsets(WindowInsets.Type.statusBars()).top;
            }
            target.setPadding(left, top + insetTop, right, bottom);
            return insets;
        });
        view.requestApplyInsets();
    }

    private static void applyLightBarFlags(View decor, boolean dark) {
        if (Build.VERSION.SDK_INT < 23) return;
        int flags = decor.getSystemUiVisibility();
        if (dark) flags &= ~View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
        else flags |= View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
        if (Build.VERSION.SDK_INT >= 26) {
            if (dark) flags &= ~View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
            else flags |= View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
        }
        decor.setSystemUiVisibility(flags);
    }
}
