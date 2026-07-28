package com.muguett.dsdcf;

import android.app.Activity;
import android.app.AlertDialog;
import android.os.Bundle;
import android.text.Editable;
import android.text.TextWatcher;
import android.view.Gravity;
import android.view.View;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ListView;
import android.widget.Spinner;
import android.widget.TextView;

import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;

import org.json.JSONException;

import java.io.File;
import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Main screen for displaying server-computed, post-close A-share results. */
public final class MainActivity extends Activity {
    private static final String STATE_PENDING_INSTALL = "pending_verified_install";
    private static final String STATE_UPDATE_VERSION_CODE = "pending_update_version_code";
    private static final String STATE_UPDATE_VERSION_NAME = "pending_update_version_name";
    private static final String STATE_UPDATE_URL = "pending_update_url";
    private static final String STATE_UPDATE_SHA256 = "pending_update_sha256";
    private static final String STATE_UPDATE_SIGNER_SHA256 = "pending_update_signer_sha256";
    private static final String STATE_UPDATE_SIZE = "pending_update_size";
    private static final ExecutorService NETWORK_EXECUTOR = Executors.newSingleThreadExecutor();
    private MarketRepository repository;
    private MarketRepository.MarketData marketData;
    private final List<MarketRepository.MarketEntry> visibleEntries = new ArrayList<>();

    private TextView marketSummary;
    private TextView operationStatus;
    private TextView resultsStatus;
    private TextView emptyState;
    private EditText search;
    private Spinner mode;
    private Spinner typeMode;
    private ArrayAdapter<String> listAdapter;
    private Button refreshButton;
    private Button updateButton;
    private File pendingInstallApk;
    private MarketRepository.UpdateInfo pendingInstallInfo;
    private boolean destroyed;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        repository = new MarketRepository(this);
        if (savedInstanceState != null && savedInstanceState.getBoolean(STATE_PENDING_INSTALL, false)) {
            File restored = new File(new File(getCacheDir(), "updates"), "DS_DCF-update.apk");
            MarketRepository.UpdateInfo restoredInfo = restoreUpdateInfo(savedInstanceState);
            if (restored.isFile() && restoredInfo != null) {
                pendingInstallApk = restored;
                pendingInstallInfo = restoredInfo;
            }
        }
        setContentView(buildScreen());
        loadCachedData();
    }

    private View buildScreen() {
        int padding = dp(14);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(padding, padding, padding, padding);
        ViewCompat.setOnApplyWindowInsetsListener(root, (view, insets) -> {
            androidx.core.graphics.Insets systemBars =
                    insets.getInsets(WindowInsetsCompat.Type.systemBars());
            view.setPadding(
                    padding + systemBars.left,
                    padding + systemBars.top,
                    padding + systemBars.right,
                    padding + systemBars.bottom
            );
            return insets;
        });

        TextView title = new TextView(this);
        title.setText(R.string.screen_title);
        title.setTextSize(22);
        root.addView(title);

        TextView notice = new TextView(this);
        notice.setText(R.string.screen_notice);
        notice.setTextSize(14);
        notice.setPadding(0, dp(8), 0, dp(8));
        root.addView(notice);

        LinearLayout actions = new LinearLayout(this);
        actions.setOrientation(LinearLayout.HORIZONTAL);
        refreshButton = new Button(this);
        refreshButton.setText(R.string.refresh_data);
        refreshButton.setOnClickListener(view -> refreshData());
        actions.addView(refreshButton, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1));
        updateButton = new Button(this);
        updateButton.setText(R.string.check_update);
        updateButton.setOnClickListener(view -> checkForUpdate());
        actions.addView(updateButton, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1));
        root.addView(actions);

        operationStatus = new TextView(this);
        operationStatus.setText(R.string.status_loading_cache);
        operationStatus.setTextSize(14);
        operationStatus.setPadding(0, dp(8), 0, dp(4));
        root.addView(operationStatus);

        marketSummary = new TextView(this);
        marketSummary.setText(R.string.market_summary_empty);
        marketSummary.setTextSize(14);
        marketSummary.setPadding(dp(8), dp(8), dp(8), dp(8));
        marketSummary.setBackgroundResource(android.R.drawable.list_selector_background);
        marketSummary.setOnClickListener(view -> showCoverageSummary());
        root.addView(marketSummary);

        search = new EditText(this);
        search.setHint(R.string.search_hint);
        search.setSingleLine(true);
        search.addTextChangedListener(new SimpleTextWatcher(this::applyFilters));
        root.addView(search);

        mode = new Spinner(this);
        ArrayAdapter<String> modeAdapter = new ArrayAdapter<>(
                this,
                android.R.layout.simple_spinner_dropdown_item,
                getResources().getStringArray(R.array.display_modes)
        );
        mode.setAdapter(modeAdapter);
        mode.setOnItemSelectedListener(new SimpleItemSelectedListener(this::applyFilters));
        root.addView(mode);

        typeMode = new Spinner(this);
        ArrayAdapter<String> typeModeAdapter = new ArrayAdapter<>(
                this,
                android.R.layout.simple_spinner_dropdown_item,
                getResources().getStringArray(R.array.type_filters)
        );
        typeMode.setAdapter(typeModeAdapter);
        typeMode.setOnItemSelectedListener(new SimpleItemSelectedListener(this::applyFilters));
        root.addView(typeMode);

        resultsStatus = new TextView(this);
        resultsStatus.setText(R.string.results_waiting);
        resultsStatus.setTextSize(15);
        resultsStatus.setPadding(0, dp(8), 0, dp(4));
        root.addView(resultsStatus);

        FrameLayout resultArea = new FrameLayout(this);
        ListView list = new ListView(this);
        listAdapter = new ArrayAdapter<>(this, android.R.layout.simple_list_item_1, android.R.id.text1, new ArrayList<>());
        listAdapter.setNotifyOnChange(false);
        list.setAdapter(listAdapter);
        list.setOnItemClickListener((parent, view, position, id) -> showEntryDetails(visibleEntries.get(position)));
        resultArea.addView(list, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT
        ));

        emptyState = new TextView(this);
        emptyState.setText(R.string.results_empty);
        emptyState.setTextSize(16);
        emptyState.setGravity(Gravity.CENTER);
        emptyState.setPadding(dp(18), dp(18), dp(18), dp(18));
        resultArea.addView(emptyState, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT
        ));
        list.setEmptyView(emptyState);
        root.addView(resultArea, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1
        ));

        TextView footer = new TextView(this);
        footer.setGravity(Gravity.CENTER_HORIZONTAL);
        footer.setText(getString(R.string.footer_version, BuildConfig.VERSION_NAME));
        footer.setTextSize(12);
        footer.setPadding(0, dp(6), 0, 0);
        root.addView(footer);
        return root;
    }

    private void loadCachedData() {
        runInBackground(() -> repository.loadCached(), data -> {
            marketData = data;
            renderSummary();
            operationStatus.setText(R.string.status_cached);
            applyFilters();
            refreshData();
        }, error -> {
            operationStatus.setText(friendlyMessage(error));
            refreshData();
        });
    }

    private void refreshData() {
        setActionsEnabled(false);
        operationStatus.setText(R.string.status_refreshing);
        runInBackground(() -> repository.refresh(status -> runOnUiThread(() -> {
            if (!destroyed) {
                operationStatus.setText(status);
            }
        })), data -> {
            marketData = data;
            renderSummary();
            operationStatus.setText(getString(R.string.status_refreshed, data.marketAsOf));
            applyFilters();
            setActionsEnabled(true);
        }, error -> {
            operationStatus.setText(getString(R.string.status_refresh_failed, friendlyMessage(error)));
            setActionsEnabled(true);
        });
    }

    private void checkForUpdate() {
        setActionsEnabled(false);
        operationStatus.setText(R.string.status_checking_update);
        runInBackground(() -> repository.checkForUpdate(), update -> {
            setActionsEnabled(true);
            if (update == null) {
                operationStatus.setText(R.string.status_latest_app);
                return;
            }
            operationStatus.setText(getString(R.string.status_update_found, update.versionName));
            new AlertDialog.Builder(this)
                    .setTitle(R.string.update_dialog_title)
                    .setMessage(getString(R.string.update_dialog_message, update.versionName))
                    .setNegativeButton(R.string.cancel, null)
                    .setPositiveButton(R.string.download_install, (dialog, ignored) -> downloadAndInstall(update))
                    .show();
        }, error -> {
            setActionsEnabled(true);
            operationStatus.setText(isHttpNotFound(error)
                    ? getString(R.string.status_no_published_update)
                    : getString(R.string.status_update_check_failed, friendlyMessage(error)));
        });
    }

    private void downloadAndInstall(MarketRepository.UpdateInfo update) {
        setActionsEnabled(false);
        operationStatus.setText(getString(R.string.status_downloading_update, update.versionName));
        runInBackground(() -> {
            File apk = MarketRepository.downloadApk(this, update);
            return UpdateInstaller.validateDownloadedApk(this, apk, update);
        }, apk -> {
            setActionsEnabled(true);
            pendingInstallApk = apk;
            pendingInstallInfo = update;
            attemptPendingInstall(true);
        }, error -> {
            setActionsEnabled(true);
            operationStatus.setText(getString(R.string.status_update_cancelled, friendlyMessage(error)));
        });
    }

    private void renderSummary() {
        if (marketData == null) {
            return;
        }
        marketSummary.setText(getString(
                R.string.market_summary,
                marketData.marketAsOf,
                marketData.companyCount,
                marketData.triggeredCompanyCount,
                marketData.conditionalCompanyCount,
                marketData.pendingCompanyCount
        ));
    }

    private void showCoverageSummary() {
        if (marketData == null) {
            return;
        }
        new AlertDialog.Builder(this)
                .setTitle(R.string.coverage_dialog_title)
                .setMessage(marketData.typeCoverageSummary())
                .setPositiveButton(R.string.close, null)
                .show();
    }

    private void applyFilters() {
        if (marketData == null || listAdapter == null) {
            return;
        }
        String keyword = search.getText().toString().trim().toLowerCase(Locale.ROOT);
        int selectedMode = mode.getSelectedItemPosition();
        int selectedType = typeMode.getSelectedItemPosition();
        visibleEntries.clear();
        for (MarketRepository.MarketEntry entry : marketData.entries) {
            if (!MarketRepository.matchesSignalFilter(
                    entry.buyTypes,
                    entry.conditionalTypes,
                    entry.pendingTypes,
                    entry.typeScores,
                    selectedMode,
                    selectedType
            )) {
                continue;
            }
            if (!keyword.isEmpty() && !entry.searchText.contains(keyword)) {
                continue;
            }
            visibleEntries.add(entry);
        }
        listAdapter.clear();
        List<String> labels = new ArrayList<>(visibleEntries.size());
        for (MarketRepository.MarketEntry entry : visibleEntries) {
            labels.add(entry.displayLabel());
        }
        listAdapter.addAll(labels);
        listAdapter.notifyDataSetChanged();
        String selectedModeLabel = String.valueOf(mode.getSelectedItem());
        String selectedTypeLabel = String.valueOf(typeMode.getSelectedItem());
        resultsStatus.setText(getString(
                R.string.results_count,
                selectedModeLabel,
                selectedTypeLabel,
                visibleEntries.size()
        ));
        emptyState.setText(keyword.isEmpty()
                ? R.string.results_empty
                : R.string.results_empty_search);
    }

    private void showEntryDetails(MarketRepository.MarketEntry entry) {
        new AlertDialog.Builder(this)
                .setTitle(entry.name + " " + entry.code)
                .setMessage(entry.detailedLabel())
                .setPositiveButton(R.string.close, null)
                .show();
    }

    private void setActionsEnabled(boolean enabled) {
        refreshButton.setEnabled(enabled);
        updateButton.setEnabled(enabled);
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private static String friendlyMessage(Exception error) {
        String detail = error.getMessage();
        if (error instanceof JSONException) {
            return "服务器返回的数据格式不正确，已拒绝使用。";
        }
        if (detail != null && detail.matches(".*[\\u4e00-\\u9fff].*")) {
            return detail;
        }
        return "操作没有完成，请检查网络后重试。";
    }

    private static boolean isHttpNotFound(Exception error) {
        Throwable current = error;
        while (current != null) {
            String detail = current.getMessage();
            if (detail != null && detail.contains("HTTP 404")) {
                return true;
            }
            current = current.getCause();
        }
        return false;
    }

    private <T> void runInBackground(CheckedSupplier<T> action, Success<T> success, Failure failure) {
        NETWORK_EXECUTOR.execute(() -> {
            try {
                T result = action.get();
                runOnUiThread(() -> {
                    if (!destroyed) {
                        success.accept(result);
                    }
                });
            } catch (Exception error) {
                runOnUiThread(() -> {
                    if (!destroyed) {
                        failure.accept(error);
                    }
                });
            } catch (OutOfMemoryError memoryFailure) {
                IOException error = new IOException("本机内存不足，无法安全打开这批市场数据。", memoryFailure);
                runOnUiThread(() -> {
                    if (!destroyed) {
                        failure.accept(error);
                    }
                });
            }
        });
    }

    private void attemptPendingInstall(boolean allowPermissionPrompt) {
        if (pendingInstallApk == null || pendingInstallInfo == null || !pendingInstallApk.isFile()) {
            clearPendingInstall();
            operationStatus.setText(R.string.status_update_file_missing);
            return;
        }
        try {
            UpdateInstaller.validateDownloadedApk(this, pendingInstallApk, pendingInstallInfo);
            if (!allowPermissionPrompt && !getPackageManager().canRequestPackageInstalls()) {
                operationStatus.setText(R.string.status_allow_install);
                return;
            }
            if (UpdateInstaller.requestInstall(this, pendingInstallApk)) {
                clearPendingInstall();
                operationStatus.setText(R.string.status_confirm_install);
            } else {
                operationStatus.setText(R.string.status_allow_install);
            }
        } catch (IOException error) {
            clearPendingInstall();
            operationStatus.setText(getString(R.string.status_update_cancelled, friendlyMessage(error)));
        }
    }

    private void clearPendingInstall() {
        pendingInstallApk = null;
        pendingInstallInfo = null;
    }

    private static MarketRepository.UpdateInfo restoreUpdateInfo(Bundle state) {
        long versionCode = state.getLong(STATE_UPDATE_VERSION_CODE, -1L);
        String versionName = state.getString(STATE_UPDATE_VERSION_NAME, "");
        String url = state.getString(STATE_UPDATE_URL, "");
        String sha256 = state.getString(STATE_UPDATE_SHA256, "");
        String signerSha256 = state.getString(STATE_UPDATE_SIGNER_SHA256, "");
        long size = state.getLong(STATE_UPDATE_SIZE, -1L);
        if (versionCode <= BuildConfig.VERSION_CODE
                || versionName == null
                || versionName.trim().isEmpty()
                || !MarketRepository.isTrustedDownloadUrl(url)
                || sha256 == null
                || !sha256.matches("[0-9a-f]{64}")
                || signerSha256 == null
                || !MarketRepository.isExpectedReleaseSignerHash(signerSha256)
                || size <= 0L) {
            return null;
        }
        return new MarketRepository.UpdateInfo(
                versionCode,
                versionName,
                url,
                sha256,
                signerSha256,
                size
        );
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (pendingInstallApk != null
                && pendingInstallInfo != null
                && pendingInstallApk.isFile()
                && getPackageManager().canRequestPackageInstalls()) {
            attemptPendingInstall(false);
        }
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        boolean hasPendingInstall = pendingInstallApk != null
                && pendingInstallApk.isFile()
                && pendingInstallInfo != null;
        outState.putBoolean(STATE_PENDING_INSTALL, hasPendingInstall);
        if (hasPendingInstall) {
            outState.putLong(STATE_UPDATE_VERSION_CODE, pendingInstallInfo.versionCode);
            outState.putString(STATE_UPDATE_VERSION_NAME, pendingInstallInfo.versionName);
            outState.putString(STATE_UPDATE_URL, pendingInstallInfo.url);
            outState.putString(STATE_UPDATE_SHA256, pendingInstallInfo.sha256);
            outState.putString(STATE_UPDATE_SIGNER_SHA256, pendingInstallInfo.signerSha256);
            outState.putLong(STATE_UPDATE_SIZE, pendingInstallInfo.size);
        }
        super.onSaveInstanceState(outState);
    }

    @Override
    protected void onDestroy() {
        destroyed = true;
        super.onDestroy();
    }

    private interface CheckedSupplier<T> { T get() throws IOException, JSONException; }
    private interface Success<T> { void accept(T value); }
    private interface Failure { void accept(Exception error); }

    private static final class SimpleTextWatcher implements TextWatcher {
        private final Runnable callback;
        SimpleTextWatcher(Runnable callback) { this.callback = callback; }
        @Override public void beforeTextChanged(CharSequence value, int start, int count, int after) { }
        @Override public void onTextChanged(CharSequence value, int start, int before, int count) { callback.run(); }
        @Override public void afterTextChanged(Editable value) { }
    }

    private static final class SimpleItemSelectedListener implements android.widget.AdapterView.OnItemSelectedListener {
        private final Runnable callback;
        SimpleItemSelectedListener(Runnable callback) { this.callback = callback; }
        @Override public void onItemSelected(android.widget.AdapterView<?> parent, View view, int position, long id) { callback.run(); }
        @Override public void onNothingSelected(android.widget.AdapterView<?> parent) { }
    }
}
