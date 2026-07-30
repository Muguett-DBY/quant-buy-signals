package com.muguett.dsdcf;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertSame;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;

import java.io.File;
import java.io.IOException;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.Signature;
import java.time.Instant;
import java.time.LocalDate;
import java.util.Arrays;
import java.util.Base64;
import java.util.Collections;
import java.util.HashMap;
import java.util.Map;

public final class MarketRepositoryContractTest {
    @Test
    public void trustedDownloadsRequireHttpsAndAnExactPinnedHost() {
        assertTrue(MarketRepository.isTrustedDownloadUrl(
                "https://github.com/Muguett-DBY/quant-buy-signals/releases/download/test/file"
        ));
        assertTrue(MarketRepository.isTrustedDownloadUrl(
                "https://release-assets.githubusercontent.com/github-production-release-asset/file"
        ));
        assertTrue(MarketRepository.isTrustedDownloadUrl(
                "https://muguett-dby.github.io/quant-buy-signals/mobile-data/manifest.json"
        ));
        assertFalse(MarketRepository.isTrustedDownloadUrl("http://github.com/file"));
        assertFalse(MarketRepository.isTrustedDownloadUrl("https://github.com.evil.example/file"));
        assertFalse(MarketRepository.isTrustedDownloadUrl("https://muguett-dby.github.io.evil.example/file"));
        assertFalse(MarketRepository.isTrustedDownloadUrl("https://evil.example/file"));
    }

    @Test
    public void stableManifestAndImmutablePayloadsHaveTwoOfficialOrigins() {
        assertEquals(
                "https://muguett-dby.github.io/quant-buy-signals/mobile-data/manifest.json",
                MarketRepository.MOBILE_MANIFEST_URL
        );
        assertEquals(
                "https://github.com/Muguett-DBY/quant-buy-signals/releases/download/mobile-market-data/",
                MarketRepository.MOBILE_RELEASE_ASSET_BASE
        );
        assertEquals(
                "https://muguett-dby.github.io/quant-buy-signals/mobile-data/",
                MarketRepository.MOBILE_PAGES_ASSET_BASE
        );
        assertEquals(
                "https://github.com/Muguett-DBY/quant-buy-signals/releases/download/android-app/android-update-manifest.sig",
                MarketRepository.UPDATE_MANIFEST_SIGNATURE_URL
        );
    }

    @Test
    public void downloadRetriesOnlyTransientFailures() {
        assertTrue(MarketRepository.isRetryableDownloadFailure(new IOException("timeout")));
        assertTrue(MarketRepository.isRetryableDownloadFailure(new IOException("下载服务器返回 HTTP 429。")));
        assertTrue(MarketRepository.isRetryableDownloadFailure(new IOException("下载服务器返回 HTTP 503。")));
        assertFalse(MarketRepository.isRetryableDownloadFailure(new IOException("下载服务器返回 HTTP 404。")));
        assertFalse(MarketRepository.isRetryableDownloadFailure(new IOException("下载文件大小不在允许范围内。")));
        assertFalse(MarketRepository.isRetryableDownloadFailure(new IOException("下载地址不是受信任的官方加密发布地址。")));
    }

    @Test
    public void updateManifestSignatureRejectsTamperingAndTheWrongKey() throws Exception {
        KeyPair trusted = generateSigningKey();
        KeyPair wrong = generateSigningKey();
        byte[] manifest = canonicalUpdateManifest(2L);
        byte[] trustedSignature = sign(trusted, manifest);
        String trustedPublicKey = Base64.getEncoder().encodeToString(trusted.getPublic().getEncoded());

        MarketRepository.UpdateInfo parsed = MarketRepository.parseSignedUpdateManifest(
                manifest,
                trustedSignature,
                trustedPublicKey
        );
        assertEquals(2L, parsed.versionCode);

        byte[] tampered = canonicalUpdateManifest(3L);
        assertSignedManifestRejected(tampered, trustedSignature, trustedPublicKey);
        assertSignedManifestRejected(manifest, sign(wrong, manifest), trustedPublicKey);
    }

    @Test
    public void authenticatedButMalformedUpdateManifestIsRejectedBeforeVersionComparison() throws Exception {
        KeyPair trusted = generateSigningKey();
        String trustedPublicKey = Base64.getEncoder().encodeToString(trusted.getPublic().getEncoded());
        byte[] malformed = new String(canonicalUpdateManifest(1L), StandardCharsets.UTF_8)
                .replace("\"apk_size\":123", "\"apk_size\":\"123\"")
                .getBytes(StandardCharsets.UTF_8);

        try {
            MarketRepository.parseSignedUpdateManifest(malformed, sign(trusted, malformed), trustedPublicKey);
            fail("a signed manifest with a coerced field type must be rejected");
        } catch (IOException expected) {
            assertTrue(expected.getMessage().contains("数字字段"));
        }
    }

    @Test
    public void releaseProvenanceMustStayOutsideTheLegacyCompatibleSignedUpdateManifest() throws Exception {
        KeyPair trusted = generateSigningKey();
        String trustedPublicKey = Base64.getEncoder().encodeToString(trusted.getPublic().getEncoded());
        byte[] manifestWithGitSha = new String(canonicalUpdateManifest(2L), StandardCharsets.UTF_8)
                .replace(
                        ",\"package_id\"",
                        ",\"git_sha\":\"1234567890abcdef1234567890abcdef12345678\",\"package_id\""
                )
                .getBytes(StandardCharsets.UTF_8);

        try {
            MarketRepository.parseSignedUpdateManifest(
                    manifestWithGitSha,
                    sign(trusted, manifestWithGitSha),
                    trustedPublicKey
            );
            fail("legacy-compatible update JSON must reject additional provenance fields");
        } catch (IOException expected) {
            assertTrue(expected.getMessage().contains("字段"));
        }
    }

    @Test
    public void updateManifestWatermarkRejectsOldAndSameVersionAmbiguousReplay() throws Exception {
        File directory = Files.createTempDirectory("ds-dcf-update-watermark").toFile();
        File watermark = new File(directory, "version");
        String firstHash = "a".repeat(64);
        String changedHash = "b".repeat(64);

        MarketRepository.UpdateManifestWatermark first = MarketRepository.acceptUpdateManifestWatermark(
                watermark,
                1L,
                3L,
                "11.3.0",
                firstHash
        );
        assertEquals(3L, first.versionCode);
        assertEquals("11.3.0", first.versionName);
        assertEquals(firstHash, first.manifestSha256);
        MarketRepository.UpdateManifestWatermark repeated = MarketRepository.acceptUpdateManifestWatermark(
                watermark,
                1L,
                3L,
                "11.3.0",
                firstHash
        );
        assertEquals(firstHash, repeated.manifestSha256);

        try {
            MarketRepository.acceptUpdateManifestWatermark(watermark, 1L, 3L, "11.3.0", changedHash);
            fail("the same versionCode must not be rebound to different signed manifest content");
        } catch (IOException expected) {
            assertTrue(expected.getMessage().contains("同一应用版本"));
        }
        assertEquals(firstHash, MarketRepository.readUpdateManifestWatermark(watermark).manifestSha256);
        try {
            MarketRepository.acceptUpdateManifestWatermark(watermark, 1L, 3L, "11.3.1", firstHash);
            fail("the same versionCode must not be rebound to a different versionName");
        } catch (IOException expected) {
            assertTrue(expected.getMessage().contains("同一应用版本"));
        }
        try {
            MarketRepository.acceptUpdateManifestWatermark(watermark, 1L, 2L, "11.2.0", changedHash);
            fail("a signed older update manifest must not look like the latest version");
        } catch (IOException expected) {
            assertTrue(expected.getMessage().contains("拒绝回退"));
        }

        MarketRepository.UpdateManifestWatermark higher = MarketRepository.acceptUpdateManifestWatermark(
                watermark,
                1L,
                4L,
                "11.4.0",
                changedHash
        );
        assertEquals(4L, higher.versionCode);
        assertEquals(changedHash, MarketRepository.readUpdateManifestWatermark(watermark).manifestSha256);
    }

    @Test
    public void damagedOrPartialUpdateManifestWatermarkFailsClosed() throws Exception {
        File directory = Files.createTempDirectory("ds-dcf-damaged-update-watermark").toFile();
        File watermark = new File(directory, "version");

        MarketRepository.UpdateManifestWatermark firstInstall = MarketRepository.acceptUpdateManifestWatermark(
                watermark,
                1L,
                1L,
                "11.2.0",
                "c".repeat(64)
        );
        assertEquals(1L, firstInstall.versionCode);
        assertTrue(watermark.isFile());

        Files.write(watermark.toPath(), "damaged\n".getBytes(StandardCharsets.US_ASCII));
        try {
            MarketRepository.acceptUpdateManifestWatermark(
                    watermark,
                    1L,
                    4L,
                    "11.4.0",
                    "d".repeat(64)
            );
            fail("a damaged update watermark must fail closed");
        } catch (IOException expected) {
            assertTrue(expected.getMessage().contains("已损坏"));
        }
        assertEquals(
                "damaged\n",
                new String(Files.readAllBytes(watermark.toPath()), StandardCharsets.US_ASCII)
        );

        Files.write(watermark.toPath(), "4\n11.4.0\n".getBytes(StandardCharsets.US_ASCII));
        try {
            MarketRepository.acceptUpdateManifestWatermark(
                    watermark,
                    1L,
                    5L,
                    "11.5.0",
                    "e".repeat(64)
            );
            fail("a partially written update watermark must fail closed");
        } catch (IOException expected) {
            assertTrue(expected.getMessage().contains("已损坏"));
        }
        assertEquals(
                "4\n11.4.0\n",
                new String(Files.readAllBytes(watermark.toPath()), StandardCharsets.US_ASCII)
        );
    }

    @Test
    public void marketWatermarkPersistsHashAndRejectsSameTimeDifferentContent() throws Exception {
        LocalDate marketDate = LocalDate.parse("2026-07-21");
        Instant timestamp = Instant.parse("2026-07-21T08:30:00Z");
        MarketRepository.SnapshotWatermark accepted = new MarketRepository.SnapshotWatermark(
                marketDate,
                timestamp,
                "a".repeat(64)
        );
        File directory = Files.createTempDirectory("ds-dcf-market-watermark").toFile();
        File file = new File(directory, "accepted-watermark");
        MarketRepository.writeSnapshotWatermark(file, accepted);
        MarketRepository.SnapshotWatermark restored = MarketRepository.readSnapshotWatermark(file);

        assertNotNull(restored);
        assertEquals(accepted.marketDate, restored.marketDate);
        assertEquals(accepted.dataTimestamp, restored.dataTimestamp);
        assertEquals(accepted.manifestSha256, restored.manifestSha256);

        MarketRepository.SnapshotWatermark conflicting = new MarketRepository.SnapshotWatermark(
                marketDate,
                timestamp,
                "b".repeat(64)
        );
        try {
            MarketRepository.selectSnapshotWatermark(restored, conflicting);
            fail("the same signed time watermark must not be rebound to different content");
        } catch (IOException expected) {
            assertTrue(expected.getMessage().contains("不同内容"));
        }

        MarketRepository.SnapshotWatermark older = new MarketRepository.SnapshotWatermark(
                LocalDate.parse("2026-07-20"),
                Instant.parse("2026-07-20T08:30:00Z"),
                "c".repeat(64)
        );
        assertSame(restored, MarketRepository.selectSnapshotWatermark(restored, older));
    }

    @Test
    public void signedMobileAssetsMustBelongToOneGeneration() throws Exception {
        String generation = "0123456789abcdef";
        MarketRepository.validateGenerationFilenames(
                "manifest-" + generation + ".sig",
                "catalog-" + generation + ".json.gz",
                "signals-" + generation + ".json.gz"
        );

        try {
            MarketRepository.validateGenerationFilenames(
                    "manifest-" + generation + ".sig",
                    "catalog-fedcba9876543210.json.gz",
                    "signals-" + generation + ".json.gz"
            );
            fail("mixed generations must be rejected");
        } catch (IOException expected) {
            assertTrue(expected.getMessage().contains("不同批次"));
        }
    }

    @Test
    public void malformedGenerationNamesFailClosedAsCheckedDataErrors() {
        try {
            MarketRepository.validateGenerationFilenames(
                    "manifest-x.sig",
                    "catalog-x.json.gz",
                    "signals-x.json.gz"
            );
            fail("malformed generation names must be rejected");
        } catch (IOException expected) {
            assertTrue(expected.getMessage().contains("无效"));
        }
    }

    @Test
    public void triggeredConditionalObserveAndApplicableCompaniesRemainBrowsable() {
        Map<String, MarketRepository.TypeScore> scores = typeScores(
                "type1", "triggered",
                "type2", "not_triggered",
                "type6", "conditional",
                "type7", "observe"
        );
        assertTrue(MarketRepository.matchesSignalFilter(
                Collections.singletonList("type1"),
                Collections.singletonList("type6"),
                scores,
                0,
                0
        ));
        assertTrue(MarketRepository.matchesSignalFilter(
                Collections.singletonList("type1"),
                Collections.singletonList("type6"),
                scores,
                1,
                0
        ));
        assertTrue(MarketRepository.matchesSignalFilter(
                Collections.singletonList("type1"),
                Collections.singletonList("type6"),
                scores,
                0,
                1
        ));
        assertTrue(MarketRepository.matchesSignalFilter(
                Collections.singletonList("type1"),
                Collections.singletonList("type6"),
                scores,
                1,
                6
        ));
        assertFalse(MarketRepository.matchesSignalFilter(
                Collections.singletonList("type1"),
                Collections.singletonList("type6"),
                scores,
                1,
                1
        ));
        assertTrue(MarketRepository.matchesSignalFilter(
                Collections.singletonList("type1"),
                Collections.singletonList("type6"),
                scores,
                2,
                7
        ));
        assertFalse(MarketRepository.matchesSignalFilter(
                Collections.singletonList("type1"),
                Collections.singletonList("type6"),
                scores,
                2,
                6
        ));
        assertTrue(MarketRepository.matchesSignalFilter(
                Collections.singletonList("type1"),
                Collections.singletonList("type6"),
                scores,
                3,
                2
        ));
        assertFalse(MarketRepository.matchesSignalFilter(
                Collections.singletonList("type1"),
                Collections.singletonList("type6"),
                scores,
                3,
                3
        ));
    }

    @Test
    public void typeCoverageSummaryShowsObserveCompaniesInsteadOfAnApparentZero() {
        Map<String, String> names = new HashMap<>();
        Map<String, Map<String, Integer>> coverage = new HashMap<>();
        for (int number = 1; number <= 7; number++) {
            String key = "type" + number;
            names.put(key, number + "类");
            Map<String, Integer> counts = new HashMap<>();
            counts.put("triggered", number == 1 ? 2 : 0);
            counts.put("conditional", number == 6 ? 14 : 0);
            counts.put("observe", number == 7 ? 1 : 0);
            coverage.put(key, counts);
        }
        MarketRepository.MarketData data = new MarketRepository.MarketData(
                "2026-07-21",
                "2026-07-21T08:30:00Z",
                LocalDate.parse("2026-07-21"),
                Instant.parse("2026-07-21T08:30:00Z"),
                4_988,
                2,
                14,
                names,
                coverage,
                Collections.emptyList()
        );

        String summary = data.typeCoverageSummary();
        assertTrue(summary.contains("1类：实际2家，待确认0家，待补证据0家，观察0家"));
        assertTrue(summary.contains("6类：实际0家，待确认14家，待补证据0家，观察0家"));
        assertTrue(summary.contains("7类：实际0家，待确认0家，待补证据0家，观察1家"));
    }

    @Test
    public void unresolvedPossibleCandidateIsBrowsableButNeverBecomesABuySignal() {
        Map<String, MarketRepository.TypeScore> scores = typeScores(
                "type1", "insufficient_evidence"
        );
        assertTrue(MarketRepository.matchesSignalFilter(
                Collections.emptyList(),
                Collections.emptyList(),
                Collections.singletonList("type1"),
                scores,
                1,
                0
        ));
        assertTrue(MarketRepository.matchesSignalFilter(
                Collections.emptyList(),
                Collections.emptyList(),
                Collections.singletonList("type1"),
                scores,
                1,
                1
        ));
        assertFalse(MarketRepository.matchesSignalFilter(
                Collections.emptyList(),
                Collections.emptyList(),
                Collections.singletonList("type1"),
                scores,
                0,
                0
        ));

        Map<String, String> names = new HashMap<>();
        for (int number = 1; number <= 7; number++) {
            names.put("type" + number, number + "类");
        }
        MarketRepository.MarketEntry entry = new MarketRepository.MarketEntry(
                "600001",
                "待补样本",
                "制造业",
                10.0,
                Collections.emptyList(),
                Collections.emptyList(),
                Collections.singletonList("type1"),
                scores,
                names,
                null
        );
        assertFalse(entry.hasTriggeredSignal());
        assertFalse(entry.hasConditionalCandidate());
        assertTrue(entry.hasPendingEvidenceCandidate());
        assertTrue(entry.displayLabel().contains("待补证据：1类（不是买入信号）"));
    }

    @Test
    public void insufficientEvidenceIsVisibleInTheListInsteadOfLookingLikeNoData() {
        Map<String, String> names = typeNames();
        MarketRepository.MarketEntry entry = new MarketRepository.MarketEntry(
                "600003",
                "资料不足样本",
                "制造业",
                10.0,
                Collections.emptyList(),
                Collections.emptyList(),
                typeScores("type4", "insufficient_evidence"),
                names,
                null
        );
        assertTrue(entry.displayLabel().contains("资料不足：4类（不是买入信号）"));
        assertFalse(entry.displayLabel().contains("未触发买入信号"));
    }

    @Test
    public void conditionalOnlyClassificationExcludesACompanyThatAlsoHasABuySignal() {
        Map<String, String> names = typeNames();
        MarketRepository.MarketEntry conditionalOnly = new MarketRepository.MarketEntry(
                "600001",
                "仅待确认",
                "制造业",
                10.0,
                Collections.emptyList(),
                Collections.singletonList("type6"),
                typeScores("type6", "conditional"),
                names,
                null
        );
        MarketRepository.MarketEntry overlap = new MarketRepository.MarketEntry(
                "600002",
                "同时触发",
                "制造业",
                10.0,
                Collections.singletonList("type1"),
                Collections.singletonList("type6"),
                typeScores("type1", "triggered", "type6", "conditional"),
                names,
                null
        );

        assertTrue(conditionalOnly.hasConditionalOnlyCandidate());
        assertFalse(overlap.hasConditionalOnlyCandidate());
    }

    @Test
    public void malformedDecisionValuesCannotMasqueradeAsALegacyCatalogue() throws Exception {
        JSONObject malformed = companyRecordWithDecisions();
        for (int number = 1; number <= 7; number++) {
            malformed.getJSONObject("types").getJSONObject("type" + number).put("decision", "malformed");
        }

        assertCompanyRecordRejected(malformed, "候选边界合同");

        JSONObject nullDecision = companyRecordWithDecisions();
        for (int number = 1; number <= 7; number++) {
            nullDecision.getJSONObject("types").getJSONObject("type" + number).put("decision", JSONObject.NULL);
        }
        assertCompanyRecordRejected(nullDecision, "候选边界合同");
    }

    @Test
    public void legacyCatalogueMayOmitPendingTypesButCannotDeclareOne() throws Exception {
        JSONObject legacy = companyRecordWithDecisions();
        for (int number = 1; number <= 7; number++) {
            legacy.getJSONObject("types").getJSONObject("type" + number).remove("decision");
        }
        legacy.remove("pending_types");
        MarketRepository.MarketEntry accepted = parseCompanyRecord(legacy);
        assertFalse(accepted.hasDecisionContract());
        assertTrue(accepted.pendingTypes.isEmpty());

        legacy.put("pending_types", new JSONArray().put("type1"));
        assertCompanyRecordRejected(legacy, "不能声明待补证据");
    }

    @Test
    public void decisionNumbersRejectStringCoercion() throws Exception {
        JSONObject stringSchema = companyRecordWithDecisions();
        stringSchema.getJSONObject("types").getJSONObject("type1")
                .getJSONObject("decision").put("schema_version", "1");
        assertCompanyRecordRejected(stringSchema, "候选边界合同");

        JSONObject stringBound = companyRecordWithDecisions();
        stringBound.getJSONObject("types").getJSONObject("type1")
                .getJSONObject("decision").put("score_lower_bound", "5.0");
        assertCompanyRecordRejected(stringBound, "候选边界合同");
    }

    @Test
    public void currentDecisionContractAcceptsConfirmedVetoWithUnneededMissingDimensions() throws Exception {
        JSONObject company = companyRecordWithDecisions();
        JSONObject type4 = company.getJSONObject("types").getJSONObject("type4");
        type4.put("status", "vetoed").put("score", 4.0);
        type4.getJSONObject("decision")
                .put("decision_complete", true)
                .put("decision_basis", "confirmed_veto")
                .put("score_lower_bound", 1.0)
                .put("score_upper_bound", 6.0)
                .put("veto_state", "confirmed")
                .put("potentially_triggerable", false)
                .put("missing_dimensions", new JSONArray().put("4a").put("4b"));

        MarketRepository.MarketEntry accepted = parseCompanyRecord(company);
        assertTrue(accepted.hasDecisionContract());
        assertTrue(accepted.pendingTypes.isEmpty());
        assertEquals("vetoed", accepted.typeScores.get("type4").status);
    }

    @Test
    public void observeOnlyCompanyIsClearlyLabelledAsNonBuyObservation() {
        Map<String, String> names = new HashMap<>();
        for (int number = 1; number <= 7; number++) {
            names.put("type" + number, number + "类");
        }
        MarketRepository.MarketEntry entry = new MarketRepository.MarketEntry(
                "600988",
                "赤峰黄金",
                "有色金属",
                25.0,
                Collections.emptyList(),
                Collections.emptyList(),
                typeScores("type7", "observe"),
                names,
                null
        );

        assertTrue(entry.hasObservedFramework());
        assertTrue(entry.displayLabel().contains("观察：7类（不是买入信号）"));
        assertFalse(entry.displayLabel().contains("买入信号："));
    }

    @Test
    public void displayLabelNeverLeaksAnInternalIndustryEnumFromAnOlderCache() {
        Map<String, String> names = new HashMap<>();
        for (int number = 1; number <= 7; number++) {
            names.put("type" + number, number + "类");
        }
        MarketRepository.MarketEntry entry = new MarketRepository.MarketEntry(
                "600519",
                "贵州茅台",
                "ALCOHOL",
                1327.5,
                Collections.emptyList(),
                Collections.emptyList(),
                typeScores(),
                names,
                null
        );

        assertTrue(entry.displayLabel().contains("未分类行业"));
        assertFalse(entry.displayLabel().contains("ALCOHOL"));
        assertFalse(entry.searchText.contains("alcohol"));
    }

    @Test
    public void companyUniverseRejectsBeijingAndNonCompanyPrefixes() {
        for (String code : Arrays.asList("000001", "300750", "600519", "688981")) {
            assertTrue(MarketRepository.isShanghaiShenzhenCompanyCode(code));
        }
        for (String code : Arrays.asList("430047", "832000", "920000", "200002", "510300", "", "60051")) {
            assertFalse(MarketRepository.isShanghaiShenzhenCompanyCode(code));
        }
    }

    @Test
    public void wholeMarketCountHasAConservativeSafetyWindow() {
        assertFalse(MarketRepository.isSafeCompanyCount(4_499));
        assertTrue(MarketRepository.isSafeCompanyCount(4_500));
        assertTrue(MarketRepository.isSafeCompanyCount(6_500));
        assertFalse(MarketRepository.isSafeCompanyCount(6_501));
    }

    @Test
    public void unknownAndDuplicateTypeKeysFailClosed() {
        assertTrue(MarketRepository.isValidTypeKeyList(Collections.emptyList()));
        assertTrue(MarketRepository.isValidTypeKeyList(Arrays.asList("type1", "type7")));
        assertFalse(MarketRepository.isValidTypeKeyList(Arrays.asList("type1", "type1")));
        assertFalse(MarketRepository.isValidTypeKeyList(Collections.singletonList("type8")));
        assertFalse(MarketRepository.isValidTypeKeyList(Collections.singletonList("")));
    }

    @Test
    public void blockedIsARecognizedNonTriggeringFrameworkStatus() {
        assertTrue(MarketRepository.isRecognizedTypeStatus("blocked"));
        assertTrue(MarketRepository.isRecognizedTypeStatus("triggered"));
        assertFalse(MarketRepository.isRecognizedTypeStatus("invalid"));
    }

    @Test
    public void legacyPlaceholderScoresAreHiddenForScorelessFrameworkStates() {
        MarketRepository.TypeScore insufficient = new MarketRepository.TypeScore(
                "insufficient_evidence",
                0.9,
                "缺少独立市场冷度证据"
        );
        MarketRepository.TypeScore notApplicable = new MarketRepository.TypeScore(
                "not_applicable",
                0.0,
                "当前框架不适用"
        );
        MarketRepository.TypeScore observed = new MarketRepository.TypeScore(
                "observe",
                5.8,
                "继续观察"
        );

        assertEquals(null, insufficient.score);
        assertEquals(null, notApplicable.score);
        assertEquals("资料不足；缺少独立市场冷度证据", insufficient.describe());
        assertEquals("不适用；当前框架不适用", notApplicable.describe());
        assertEquals(Double.valueOf(5.8), observed.score);
        assertEquals("观察，5.8分；继续观察", observed.describe());
    }

    @Test
    public void companyDetailsShowKnownDimensionScoresAndNameOnlyTheMissingDimension() {
        MarketRepository.DecisionSummary decision = new MarketRepository.DecisionSummary(
                false,
                "unresolved_missing_evidence",
                5.0,
                7.0,
                "none",
                true,
                Collections.singletonList("1c")
        );
        Map<String, Double> scores = new HashMap<>();
        scores.put("1a", 8.0);
        scores.put("1b", 7.0);
        scores.put("1d", 6.0);
        Map<String, String> reasons = new HashMap<>();
        reasons.put("1a", "买入区内折价");
        reasons.put("1b", "陷阱排查通过");
        reasons.put("1d", "业绩拐点催化");
        MarketRepository.TypeScore typeScore = new MarketRepository.TypeScore(
                "insufficient_evidence",
                null,
                "安全边际资料待补",
                decision,
                scores,
                reasons
        );

        assertEquals("8.0分；买入区内折价", typeScore.describeDimension("1a"));
        assertEquals("资料不足（该项尚缺可核验数据）", typeScore.describeDimension("1c"));
        assertEquals("数据版本过旧，请获取最新数据", typeScore.describeDimension("1z"));
    }

    @Test
    public void type6InvestorActionDimensionIsNeverDisplayedAsMissingCompanyData() {
        MarketRepository.DecisionSummary inactiveDecision = new MarketRepository.DecisionSummary(
                false,
                "conservative_upper_bound",
                3.0,
                6.0,
                "none",
                false,
                Collections.singletonList("6e")
        );
        MarketRepository.TypeScore inactive = new MarketRepository.TypeScore(
                "observe",
                null,
                "尚未达到其它前置条件",
                inactiveDecision,
                Collections.emptyMap(),
                Collections.emptyMap(),
                Collections.singleton("6e")
        );
        assertEquals("当前无需确认仓位（投资者动作，不是公司资料缺失）", inactive.describeDimension("6e"));

        MarketRepository.DecisionSummary activeDecision = new MarketRepository.DecisionSummary(
                false,
                "action_condition",
                7.0,
                8.0,
                "none",
                true,
                Collections.singletonList("6e")
        );
        MarketRepository.TypeScore active = new MarketRepository.TypeScore(
                "conditional",
                null,
                "等待仓位确认",
                activeDecision,
                Collections.emptyMap(),
                Collections.emptyMap(),
                Collections.singleton("6e")
        );
        assertEquals("待确认仓位（投资者动作，不是公司资料缺失）", active.describeDimension("6e"));
    }

    @Test
    public void legacyMachineReasonsAreSanitizedBeforeAnyAndroidDisplay() {
        for (String machineText : Arrays.asList(
                "证据:patch6-observable",
                "model_id=patch6-type7-quality-equity-v7",
                "schema_version=6",
                "formula=internal_formula",
                "reported_formula=(normalised_roe-g)/(cost_of_equity-g)",
                "financial_fade_horizon_not_tam_or_penetration_proof"
        )) {
            MarketRepository.TypeScore score = new MarketRepository.TypeScore("observe", 5.8, machineText);
            assertFalse(score.describe().contains(machineText));
            assertTrue(score.describe().contains("可核验的财务与行业数据"));
        }

        MarketRepository.TypeScore type2 = new MarketRepository.TypeScore(
                "insufficient_evidence",
                null,
                "证据:patch6-type2c-qua"
        );
        MarketRepository.TypeScore readable = new MarketRepository.TypeScore(
                "observe",
                5.8,
                "净10/毛10/现8/ROIC10"
        );
        assertEquals("资料不足；量价与换手数据", type2.describe());
        assertEquals("观察，5.8分；净10/毛10/现8/ROIC10", readable.describe());
    }

    @Test
    public void legacyDetailTextCannotLeakModelContractsFromAnOlderCache() {
        Map<String, String> names = new HashMap<>();
        for (int number = 1; number <= 7; number++) {
            names.put("type" + number, number + "类");
        }
        MarketRepository.MarketEntry entry = new MarketRepository.MarketEntry(
                "600519",
                "贵州茅台",
                "白酒",
                1327.5,
                Collections.emptyList(),
                Collections.emptyList(),
                typeScores(),
                names,
                "普通说明\nmodel_id=patch6-type7-quality-equity-v7\nschema_version=7"
        );

        String detail = entry.detailedLabel();
        assertTrue(detail.contains("可核验的财务与行业数据"));
        assertFalse(detail.contains("model_id"));
        assertFalse(detail.contains("schema_version"));
        assertFalse(detail.contains("patch6"));
    }

    @Test
    public void restoredUpdateMetadataAcceptsOnlyThePinnedReleaseCertificate() {
        assertTrue(MarketRepository.isExpectedReleaseSignerHash(
                "e818fa2a0d18b12316e826bdaeb1877a62ccb68634b42fdd598c687a74293369"
        ));
        assertFalse(MarketRepository.isExpectedReleaseSignerHash("0".repeat(64)));
        assertFalse(MarketRepository.isExpectedReleaseSignerHash(null));
    }

    private static JSONObject companyRecordWithDecisions() throws Exception {
        JSONObject company = new JSONObject()
                .put("code", "600001")
                .put("name", "合同样本")
                .put("industry", "制造业")
                .put("price", 10.0)
                .put("buy_types", new JSONArray())
                .put("conditional_types", new JSONArray())
                .put("pending_types", new JSONArray());
        JSONObject types = new JSONObject();
        for (int number = 1; number <= 7; number++) {
            JSONObject decision = new JSONObject()
                    .put("schema_version", 1)
                    .put("model_id", "buy-decision-bounds-v1")
                    .put("decision_complete", true)
                    .put("decision_basis", "full_evidence")
                    .put("score_lower_bound", 5.0)
                    .put("score_upper_bound", 5.0)
                    .put("veto_state", "none")
                    .put("potentially_triggerable", false)
                    .put("missing_dimensions", new JSONArray());
            types.put(
                    "type" + number,
                    new JSONObject()
                            .put("status", "not_triggered")
                            .put("score", 5.0)
                            .put("reason", "")
                            .put("decision", decision)
            );
        }
        return company.put("types", types);
    }

    private static Map<String, String> typeNames() {
        Map<String, String> names = new HashMap<>();
        for (int number = 1; number <= 7; number++) {
            names.put("type" + number, number + "类");
        }
        return names;
    }

    private static MarketRepository.MarketEntry parseCompanyRecord(JSONObject company) throws Exception {
        Method method = MarketRepository.class.getDeclaredMethod(
                "parseEntry",
                JSONObject.class,
                Map.class,
                String.class,
                boolean.class
        );
        method.setAccessible(true);
        try {
            return (MarketRepository.MarketEntry) method.invoke(null, company, typeNames(), null, false);
        } catch (InvocationTargetException exception) {
            Throwable cause = exception.getCause();
            if (cause instanceof IOException) {
                throw (IOException) cause;
            }
            throw exception;
        }
    }

    private static void assertCompanyRecordRejected(JSONObject company, String expectedMessage) throws Exception {
        try {
            parseCompanyRecord(company);
            fail("invalid company decision contract must be rejected");
        } catch (IOException expected) {
            assertTrue(expected.getMessage().contains(expectedMessage));
        }
    }

    private static KeyPair generateSigningKey() throws Exception {
        KeyPairGenerator generator = KeyPairGenerator.getInstance("EC");
        generator.initialize(256);
        return generator.generateKeyPair();
    }

    private static Map<String, MarketRepository.TypeScore> typeScores(String... overrides) {
        Map<String, MarketRepository.TypeScore> scores = new HashMap<>();
        for (int number = 1; number <= 7; number++) {
            scores.put("type" + number, new MarketRepository.TypeScore("not_applicable", null, ""));
        }
        for (int index = 0; index < overrides.length; index += 2) {
            scores.put(overrides[index], new MarketRepository.TypeScore(overrides[index + 1], 5.8, ""));
        }
        return scores;
    }

    private static byte[] sign(KeyPair keyPair, byte[] payload) throws Exception {
        Signature signer = Signature.getInstance("SHA256withECDSA");
        signer.initSign(keyPair.getPrivate());
        signer.update(payload);
        return signer.sign();
    }

    private static void assertSignedManifestRejected(
            byte[] manifest,
            byte[] signature,
            String publicKey
    ) throws Exception {
        try {
            MarketRepository.parseSignedUpdateManifest(manifest, signature, publicKey);
            fail("an invalid detached signature must be rejected");
        } catch (IOException expected) {
            assertTrue(expected.getMessage().contains("签名校验失败"));
        }
    }

    private static byte[] canonicalUpdateManifest(long versionCode) {
        String releaseCertificate =
                "e818fa2a0d18b12316e826bdaeb1877a62ccb68634b42fdd598c687a74293369";
        String manifest = "{\"apk_sha256\":\"" + "0".repeat(64)
                + "\",\"apk_size\":123"
                + ",\"apk_url\":\"https://github.com/Muguett-DBY/quant-buy-signals/releases/download/v11.3.0/"
                + "DS_DCF-v11.3.0-android-release.apk\""
                + ",\"package_id\":\"com.muguett.dsdcf\""
                + ",\"schema_version\":1"
                + ",\"signer_sha256\":\"" + releaseCertificate + "\""
                + ",\"version_code\":" + versionCode
                + ",\"version_name\":\"11.3.0\"}\n";
        return manifest.getBytes(StandardCharsets.UTF_8);
    }
}
