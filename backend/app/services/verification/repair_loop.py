import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable
from backend.app.schemas.migration import GeneratedFile
from backend.app.schemas.test_result import (
    TestCaseSchema, TestResultSchema, RepairAttemptSchema, VerificationReport
)
from backend.app.services.compilation.java_builder import JavaProjectBuilder
from backend.app.services.compilation.java_compiler import JavaCompiler, CompileResult
from backend.app.services.verification.differential_tester import DifferentialTester
from backend.app.services.verification.failure_analyzer import FailureAnalyzer
from backend.app.services.generation.translation_service import TranslationService
from backend.app.services.generation.prompt_builder import TranslationPromptBuilder
from backend.app.services.retrieval.hybrid_retriever import ContextBundle
from backend.app.db.database import get_connection

logger = logging.getLogger(__name__)

class RepairLoopOrchestrator:
    """
    Coordinates compilation, differential testing, failure diagnosis,
    and iterative targeted LLM repair with full regression testing.
    """
    def __init__(
        self,
        project_id: str,
        migration_id: str,
        workspace_dir: Path,
        translation_service: TranslationService,
        max_attempts: int = 3
    ):
        self.project_id = project_id
        self.migration_id = migration_id
        self.workspace_dir = workspace_dir
        self.translation_service = translation_service
        self.max_attempts = max_attempts
        self.compiler = JavaCompiler()

    def execute_verification_and_repair(
        self,
        target_name: str,
        context: ContextBundle,
        initial_files: List[GeneratedFile],
        entry_point: str,
        cobol_source: str,
        baseline: bool = False,
        on_phase_event: Optional[Callable[[int, str, str, str, Optional[Dict[str, Any]]], None]] = None
    ) -> VerificationReport:
        attempts_history: List[RepairAttemptSchema] = []
        current_files = initial_files
        final_status = "FAILED"
        compilation_passed = False
        last_test_results: List[TestResultSchema] = []

        # Generate test suite once for consistent evaluation across attempts
        test_cases = DifferentialTester.generate_test_suite(target_name)

        for attempt_no in range(1, self.max_attempts + 1):
            attempt_id = f"att_{self.migration_id}_{attempt_no}"
            attempt_dir = self.workspace_dir / "generated" / f"attempt_{attempt_no}"
            
            # Step A: Assemble Java Project
            JavaProjectBuilder.assemble_project(attempt_dir, current_files, entry_point)
            entry_file = attempt_dir / "src" / "main" / "java" / "com" / "migration" / f"{entry_point}.java"

            # Step B: Layer 1 - Compilation Check
            if on_phase_event:
                on_phase_event(8, "Compilation", "RUNNING", f"Attempt #{attempt_no}: Validating Java compilation with javac...", {"attempt": attempt_no, "code": current_files[0].content if current_files else ""})

            compile_res: CompileResult = self.compiler.compile(attempt_dir, entry_file)
            compilation_passed = compile_res.success

            if not compile_res.success:
                if on_phase_event:
                    on_phase_event(8, "Compilation", "FAILED", f"Attempt #{attempt_no}: Compilation error.", {"errors": compile_res.errors})

                diag = FailureAnalyzer.diagnose_compile_failure(compile_res.errors)
                attempt_record = RepairAttemptSchema(
                    attempt_id=attempt_id,
                    attempt_number=attempt_no,
                    compile_status="FAIL",
                    compile_errors=compile_res.errors,
                    tests_passed=0,
                    tests_total=len(test_cases),
                    status="FAILED",
                    failure_category=diag["category"],
                    diagnosis=diag["diagnosis"],
                    generated_code=current_files[0].content if current_files else "",
                    test_results=[]
                )
                attempts_history.append(attempt_record)
                self._persist_attempt(attempt_record)

                if attempt_no < self.max_attempts:
                    logger.info(f"Attempt #{attempt_no} failed compilation. Initiating repair...")
                    if on_phase_event:
                        on_phase_event(10, "Repair", "RUNNING", f"Attempt #{attempt_no}: Compiler error. Initiating repair with Gemini...", {"attempt": attempt_no})
                    repair_ctx = {
                        "attempt_number": attempt_no,
                        "compile_errors": compile_res.errors,
                        "failing_tests": []
                    }
                    repair_prompt = TranslationPromptBuilder.build_prompt(
                        project_name=self.project_id,
                        target_name=target_name,
                        context=context,
                        repair_context=repair_ctx,
                        baseline=baseline
                    )
                    trans_res = self.translation_service.translate(repair_prompt)
                    current_files = trans_res["files"]
                    if on_phase_event and current_files:
                        on_phase_event(7, "Java Generation", "SUCCESS", f"Generated repair code for attempt #{attempt_no + 1}.", {"code": current_files[0].content})
                    continue
                else:
                    final_status = "VERIFICATION FAILED"
                    if on_phase_event:
                        on_phase_event(11, "Final Verification", "FAILED", "Max repairs reached with compile failures.", {"code": current_files[0].content if current_files else ""})
                    break

            # Step C: Layer 2 - Differential Functional Testing
            if on_phase_event:
                on_phase_event(8, "Compilation", "SUCCESS", f"Attempt #{attempt_no}: Compilation passed.", {"attempt": attempt_no})
                on_phase_event(9, "Differential Testing", "RUNNING", f"Attempt #{attempt_no}: Running differential test cases COBOL vs Java...", {"attempt": attempt_no})

            tester = DifferentialTester(cobol_source, attempt_dir, entry_point)
            test_results = tester.run_suite(test_cases)
            last_test_results = test_results

            passed_count = sum(1 for tr in test_results if tr.status == "PASS")
            failing_results = [tr for tr in test_results if tr.status != "PASS"]

            if not failing_results:
                # All tests passed! Fully verified!
                final_status = "VERIFIED"
                if on_phase_event:
                    on_phase_event(9, "Differential Testing", "SUCCESS", f"100% equivalence ({passed_count}/{len(test_cases)} tests passed).", {"attempt": attempt_no})
                    on_phase_event(11, "Final Verification", "SUCCESS", "All verification stages verified successfully!", {"code": current_files[0].content if current_files else ""})

                attempt_record = RepairAttemptSchema(
                    attempt_id=attempt_id,
                    attempt_number=attempt_no,
                    compile_status="PASS",
                    compile_errors=None,
                    tests_passed=passed_count,
                    tests_total=len(test_cases),
                    status="VERIFIED",
                    failure_category=None,
                    diagnosis="Functional equivalence verified on all boundary, fuzz, and standard cases.",
                    generated_code=current_files[0].content if current_files else "",
                    test_results=test_results
                )
                attempts_history.append(attempt_record)
                self._persist_attempt(attempt_record)
                break
            else:
                diag = FailureAnalyzer.diagnose_test_failures(failing_results)
                attempt_record = RepairAttemptSchema(
                    attempt_id=attempt_id,
                    attempt_number=attempt_no,
                    compile_status="PASS",
                    compile_errors=None,
                    tests_passed=passed_count,
                    tests_total=len(test_cases),
                    status="FAILED",
                    failure_category=diag["category"],
                    diagnosis=diag["diagnosis"],
                    generated_code=current_files[0].content if current_files else "",
                    test_results=test_results
                )
                attempts_history.append(attempt_record)
                self._persist_attempt(attempt_record)

                if attempt_no < self.max_attempts:
                    logger.info(f"Attempt #{attempt_no} failed functional tests ({passed_count}/{len(test_cases)} passed). Initiating repair...")
                    if on_phase_event:
                        on_phase_event(9, "Differential Testing", "FAILED", f"Attempt #{attempt_no}: {passed_count}/{len(test_cases)} tests passed.", {"attempt": attempt_no})
                        on_phase_event(10, "Repair", "RUNNING", f"Attempt #{attempt_no} failed tests. Initiating repair #{attempt_no + 1} with Gemini...", {"attempt": attempt_no})
                    repair_ctx = {
                        "attempt_number": attempt_no,
                        "compile_errors": None,
                        "failing_tests": [
                            {"input": f.input_data, "expected": f.cobol_output, "actual": f.java_output}
                            for f in failing_results
                        ]
                    }
                    repair_prompt = TranslationPromptBuilder.build_prompt(
                        project_name=self.project_id,
                        target_name=target_name,
                        context=context,
                        repair_context=repair_ctx,
                        baseline=baseline
                    )
                    trans_res = self.translation_service.translate(repair_prompt)
                    current_files = trans_res["files"]
                    if on_phase_event and current_files:
                        on_phase_event(7, "Java Generation", "SUCCESS", f"Generated repair code for attempt #{attempt_no + 1}.", {"code": current_files[0].content})
                else:
                    final_status = "VERIFICATION FAILED"
                    if on_phase_event:
                        on_phase_event(9, "Differential Testing", "FAILED", f"Attempt #{attempt_no}: {passed_count}/{len(test_cases)} tests passed.", {"attempt": attempt_no})
                        on_phase_event(11, "Final Verification", "FAILED", "Max repairs reached without full equivalence.", {"code": current_files[0].content if current_files else ""})

        total_tests = len(test_cases)
        passed_tests = attempts_history[-1].tests_passed if attempts_history else 0
        eq_rate = (passed_tests / total_tests * 100.0) if total_tests > 0 else 0.0

        return VerificationReport(
            migration_id=self.migration_id,
            project_id=self.project_id,
            target_name=target_name,
            final_status=final_status,
            compilation_passed=compilation_passed,
            total_tests=total_tests,
            passed_tests=passed_tests,
            equivalence_rate=round(eq_rate, 2),
            total_repair_attempts=len(attempts_history),
            attempts_history=attempts_history,
            unsupported_constructs=[]
        )

    def _persist_attempt(self, attempt: RepairAttemptSchema):
        conn = get_connection()
        cursor = conn.cursor()
        try:
            # Ensure parent project exists
            cursor.execute("INSERT OR IGNORE INTO projects (project_id, name) VALUES (?, ?)", (self.project_id, self.project_id))
            # Ensure parent migration exists
            cursor.execute(
                "INSERT OR IGNORE INTO migrations (migration_id, project_id, target_mode, target_name, status) VALUES (?, ?, 'PROGRAM', 'TARGET', 'RUNNING')",
                (self.migration_id, self.project_id)
            )
            cursor.execute(
                """
                INSERT OR REPLACE INTO attempts
                (attempt_id, migration_id, attempt_number, generated_code, compile_status, compile_errors, tests_passed, tests_total, status, failure_category, diagnosis)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt.attempt_id,
                    self.migration_id,
                    attempt.attempt_number,
                    attempt.generated_code,
                    attempt.compile_status,
                    attempt.compile_errors,
                    attempt.tests_passed,
                    attempt.tests_total,
                    attempt.status,
                    attempt.failure_category,
                    attempt.diagnosis
                )
            )
            conn.commit()
        except Exception as e:
            logger.error(f"Error persisting attempt {attempt.attempt_id}: {e}")
        finally:
            conn.close()
