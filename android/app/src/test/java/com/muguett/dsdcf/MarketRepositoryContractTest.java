package com.muguett.dsdcf;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertSame;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import org.junit.Test;

import java.io.File;
import java.io.IOException;
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
    public void stableManifestUsesPagesWhileImmutablePayloadsRemainOnTheRelease() {
        assertEquals(
                "https://muguett-dby.github.io/quant-buy-signals/mobile-data/manifest.json",
                MarketRepository.MOBILE_MANIFEST_URL
        );
        assertEquals(
                "https://github.com/Muguett-DBY/quant-buy-signals/releases/download/mobile-market-data/",
                MarketRepository.MOBILE_RELEASE_ASSET_BASE
        );
        assertEquals(
                "https://github.com/Muguett-DBY/quant-buy-signals/releases/download/android-app/android-update-manifest.sig",
                MarketRepository.UPDATE_MANIFEST_SIGNATURE_URL
        );
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
        assertTrue(summary.contains("1类：实际2家，待确认0家，观察0家"));
        assertTrue(summary.contains("6类：实际0家，待确认14家，观察0家"));
        assertTrue(summary.contains("7类：实际0家，待确认0家，观察1家"));
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
    public void restoredUpdateMetadataAcceptsOnlyThePinnedReleaseCertificate() {
        assertTrue(MarketRepository.isExpectedReleaseSignerHash(
                "e818fa2a0d18b12316e826bdaeb1877a62ccb68634b42fdd598c687a74293369"
        ));
        assertFalse(MarketRepository.isExpectedReleaseSignerHash("0".repeat(64)));
        assertFalse(MarketRepository.isExpectedReleaseSignerHash(null));
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
                + ",\"apk_url\":\"https://github.com/Muguett-DBY/quant-buy-signals/releases/download/v11.2.0/"
                + "DS_DCF-v11.2.0-android-release.apk\""
                + ",\"package_id\":\"com.muguett.dsdcf\""
                + ",\"schema_version\":1"
                + ",\"signer_sha256\":\"" + releaseCertificate + "\""
                + ",\"version_code\":" + versionCode
                + ",\"version_name\":\"11.2.0\"}\n";
        return manifest.getBytes(StandardCharsets.UTF_8);
    }
}
