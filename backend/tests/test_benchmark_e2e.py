import unittest
import tempfile
from pathlib import Path
from backend.app.services.ingestion.workspace_manager import WorkspaceManager
from backend.app.services.benchmark.benchmark_runner import BenchmarkRunner

SAMPLE_BILLING = """
       IDENTIFICATION DIVISION.
       PROGRAM-ID. BILLING-APP.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01 WS-VARS.
          05 WS-GROSS-AMOUNT PIC 9(5)V99 VALUE 100.00.
          05 WS-TAX-RATE     PIC 9V99 VALUE 0.08.
          05 WS-TAX-AMOUNT   PIC 9(5)V99 VALUE 0.00.
          05 WS-NET-TOTAL    PIC 9(5)V99 VALUE 0.00.
       COPY TAXCONFIG.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 1000-PROCESS.
           STOP RUN.
       1000-PROCESS.
           COMPUTE WS-TAX-AMOUNT ROUNDED = WS-GROSS-AMOUNT * WS-TAX-RATE.
           COMPUTE WS-NET-TOTAL = WS-GROSS-AMOUNT + WS-TAX-AMOUNT.
"""

TAX_CONFIG_CPY = """
       01 TAX-CONFIG.
          05 DEFAULT-TAX-RATE PIC 9V99 VALUE 0.08.
"""

from backend.app.providers.base import LLMProvider, LLMResponse
from backend.app.services.generation.translation_service import TranslationService
import json

class MockTestProvider(LLMProvider):
    def generate(self, prompt: str, system_prompt = None, temperature = 0.0) -> LLMResponse:
        java_code = """package com.migration;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.Map;
import java.util.HashMap;

public class BillingApp {
    private BigDecimal grossAmount = new BigDecimal("100.00");
    private BigDecimal taxRate = new BigDecimal("0.08");
    private BigDecimal taxAmount = BigDecimal.ZERO;
    private BigDecimal netTotal = BigDecimal.ZERO;

    public Map<String, Object> execute(Map<String, Object> input) {
        if (input != null) {
            if (input.containsKey("GROSS-AMOUNT")) {
                this.grossAmount = new BigDecimal(input.get("GROSS-AMOUNT").toString());
            } else if (input.containsKey("WS-GROSS-AMOUNT")) {
                this.grossAmount = new BigDecimal(input.get("WS-GROSS-AMOUNT").toString());
            }
            if (input.containsKey("TAX-RATE")) {
                this.taxRate = new BigDecimal(input.get("TAX-RATE").toString());
            }
        }
        this.taxAmount = this.grossAmount.multiply(this.taxRate).setScale(2, RoundingMode.HALF_UP);
        this.netTotal = this.grossAmount.add(this.taxAmount);
        
        Map<String, Object> out = new HashMap<>();
        out.put("status", "OK");
        out.put("WS-GROSS-AMOUNT", this.grossAmount);
        out.put("WS-TAX-RATE", this.taxRate);
        out.put("WS-TAX-AMOUNT", this.taxAmount);
        out.put("WS-NET-TOTAL", this.netTotal);
        return out;
    }

    public static void main(String[] args) {
        BillingApp app = new BillingApp();
        Map<String, Object> input = new HashMap<>();
        for (String arg : args) {
            int eq = arg.indexOf('=');
            if (eq != -1) {
                input.put(arg.substring(0, eq), arg.substring(eq + 1));
            }
        }
        Map<String, Object> result = app.execute(input);
        System.out.println("___RESULT_JSON___");
        StringBuilder sb = new StringBuilder();
        sb.append("{");
        boolean first = true;
        for (Map.Entry<String, Object> e : result.entrySet()) {
            if (!first) sb.append(", ");
            sb.append("\\\"").append(e.getKey()).append("\\\": ");
            Object val = e.getValue();
            if (val instanceof Number) {
                sb.append(val);
            } else {
                sb.append("\\\"").append(val).append("\\\"");
            }
            first = false;
        }
        sb.append("}");
        System.out.println(sb.toString());
    }
}"""
        resp_json = {
            "files": [{"path": "BillingApp.java", "content": java_code}],
            "entry_point": "BillingApp",
            "notes": ["Test execution"],
            "unsupported_constructs": []
        }
        return LLMResponse(
            content=json.dumps(resp_json),
            model="mock-unit-test",
            prompt_tokens=10,
            completion_tokens=20,
            raw_response={}
        )

class TestBenchmarkAndEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.wm = WorkspaceManager(Path(self.tmpdir.name))
        self.proj_id = self.wm.create_project("E2E-Benchmark-Test")

        self.wm.save_file(self.proj_id, "BILLING.cob", SAMPLE_BILLING, "SOURCE")
        self.wm.save_file(self.proj_id, "TAXCONFIG.cpy", TAX_CONFIG_CPY, "COPYBOOK")
        self.summary = self.wm.analyze_and_index_project(self.proj_id)

    def tearDown(self):
        from backend.app.db.database import get_connection
        conn = get_connection()
        conn.execute("DELETE FROM projects WHERE project_id = ?", (self.proj_id,))
        conn.execute("DELETE FROM files WHERE project_id = ?", (self.proj_id,))
        conn.execute("DELETE FROM chunks WHERE project_id = ?", (self.proj_id,))
        conn.execute("DELETE FROM migrations WHERE project_id = ?", (self.proj_id,))
        conn.commit()
        conn.close()
        self.tmpdir.cleanup()

    def test_indexing_summary_metrics(self):
        self.assertEqual(self.summary.files_detected, 2)
        self.assertEqual(self.summary.copybooks_detected, 1)
        self.assertTrue(self.summary.paragraphs >= 2)
        self.assertTrue(self.summary.dependencies_count > 0)
        self.assertEqual(self.summary.index_status, "INDEXED")

    def test_benchmark_runner_execution(self):
        translator = TranslationService(provider=MockTestProvider())
        runner = BenchmarkRunner(self.wm, translator=translator)
        results = runner.run_benchmark(
            project_id=self.proj_id,
            target_name="BILLING-APP",
            query="billing and tax computation"
        )

        self.assertIn("baseline", results)
        self.assertIn("proposed", results)

        # Baseline should show compile success
        self.assertTrue(results["baseline"]["compile_success"])
        # Proposed should be functionally verified
        self.assertTrue(results["proposed"]["functional_equivalence"])

        # Check export files were created
        pdir = self.wm.get_project_dir(self.proj_id)
        self.assertTrue((pdir / "reports" / "results.json").exists())
        self.assertTrue((pdir / "reports" / "results.csv").exists())
        self.assertTrue((pdir / "reports" / "report.html").exists())

if __name__ == "__main__":
    unittest.main()
