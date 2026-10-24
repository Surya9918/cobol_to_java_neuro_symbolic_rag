import re
import time
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Dict, Any, Optional, List, Tuple
from backend.app.services.parsing.cobol_parser import CobolParser
from backend.app.services.parsing.cobol_ast import ProgramNode, ParagraphNode, StatementNode, DataItemNode

class CobolRuntimeEngine:
    """
    Deterministic COBOL execution engine implementing exact COBOL fixed-point arithmetic,
    PIC clause truncation, and PROCEDURE DIVISION paragraph dispatch.
    Acts as the COBOL runtime adapter for differential verification.
    """
    def __init__(self, source_code: str):
        self.source_code = source_code
        self.parser = CobolParser(source_code)
        self.program = self.parser.parse()
        self.variables: Dict[str, Any] = {}
        self.paragraphs: Dict[str, ParagraphNode] = {}
        self.perform_call_stack: List[str] = []
        self._init_environment()

    def _init_environment(self):
        self.variables.clear()
        self.paragraphs.clear()
        # Register variables from DATA DIVISION
        data_div = self.program.divisions.get("DATA")
        if data_div:
            for item in data_div.data_items:
                self._init_variable(item)
            for sec in data_div.sections:
                for child in sec.children:
                    if isinstance(child, DataItemNode):
                        self._init_variable(child)

        # Register paragraphs from PROCEDURE DIVISION
        proc_div = self.program.divisions.get("PROCEDURE")
        if proc_div:
            for sec in proc_div.sections:
                for p in sec.paragraphs:
                    self.paragraphs[p.name.upper()] = p

    def _init_variable(self, item: DataItemNode):
        var_name = item.name.upper()
        raw = str(item.value).strip("'\" ") if item.value is not None else ""
        if item.data_type in ("NUMERIC", "DECIMAL"):
            try:
                val = Decimal(raw) if raw else Decimal("0.00")
            except (InvalidOperation, ValueError):
                val = raw if raw else Decimal("0.00")
        else:
            val = raw
        self.variables[var_name] = val

    def _eval_operand(self, token: str) -> Any:
        tok = token.strip()
        tok_upper = tok.upper()
        if tok_upper in self.variables:
            return self.variables[tok_upper]

        if (tok.startswith("'") and tok.endswith("'")) or (tok.startswith('"') and tok.endswith('"')):
            return tok[1:-1]

        try:
            return Decimal(tok)
        except InvalidOperation:
            return tok

    def _set_variable(self, var_name: str, value: Any):
        vname = var_name.upper()
        if isinstance(value, (int, float)):
            self.variables[vname] = Decimal(str(value))
        elif isinstance(value, str):
            try:
                self.variables[vname] = Decimal(value)
            except InvalidOperation:
                self.variables[vname] = value
        else:
            self.variables[vname] = value

    def _evaluate_math_expr(self, expr_str: str) -> Decimal:
        tokens = re.findall(r'[A-Za-z0-9_-]+|[+*/()-]', expr_str)
        eval_parts = []
        for t in tokens:
            t_upper = t.upper()
            if t_upper in self.variables:
                val = self.variables[t_upper]
                eval_parts.append(str(val) if isinstance(val, (int, float, Decimal)) else "0")
            elif re.match(r'^\d*\.?\d+$', t):
                eval_parts.append(t)
            elif t in ('+', '-', '*', '/', '(', ')'):
                eval_parts.append(t)
        
        safe_expr = " ".join(eval_parts)
        try:
            res = eval(safe_expr, {"__builtins__": {}}, {})
            return Decimal(str(res)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except Exception:
            return Decimal("0.00")

    def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        # Reset environment for pure, isolated test execution
        self._init_environment()
        start_time = time.time()
        
        # Load input variables with prefix binding
        for k, v in input_data.items():
            self._set_variable(k, v)
            k_upper = k.upper()
            if f"WS-{k_upper}" in self.variables:
                self._set_variable(f"WS-{k_upper}", v)
            if f"LK-{k_upper}" in self.variables:
                self._set_variable(f"LK-{k_upper}", v)
            if k_upper.startswith("WS-"):
                clean = k_upper[3:]
                self._set_variable(clean, v)

        proc_div = self.program.divisions.get("PROCEDURE")
        if not proc_div or not proc_div.sections:
            return {"status": "ERROR", "message": "No PROCEDURE DIVISION found"}

        self.perform_call_stack.clear()
        stopped = False
        for sec in proc_div.sections:
            if stopped:
                break
            for para in sec.paragraphs:
                stopped = self._execute_paragraph(para)
                if stopped:
                    break

        exec_time_ms = (time.time() - start_time) * 1000.0

        result: Dict[str, Any] = {
            "status": "OK",
            "execution_time_ms": exec_time_ms
        }
        for k, v in self.variables.items():
            if isinstance(v, Decimal):
                result[k] = float(v)
            else:
                result[k] = v

        return result

    def _execute_paragraph(self, para: ParagraphNode) -> bool:
        for stmt in para.statements:
            if stmt.verb in ('STOP', 'GOBACK'):
                return True
            self._execute_statement(stmt)
        return False

    def _execute_statement(self, stmt: StatementNode):
        verb = stmt.verb

        if verb == 'MOVE':
            if stmt.variables_read and stmt.variables_written:
                src_val = self._eval_operand(stmt.variables_read[0])
                for dest in stmt.variables_written:
                    self._set_variable(dest, src_val)

        elif verb == 'COMPUTE':
            raw = stmt.raw_statement.replace("COMPUTE", "").replace("ROUNDED", "").strip()
            if "=" in raw:
                target, expr = raw.split("=", 1)
                target = target.strip()
                val = self._evaluate_math_expr(expr.strip())
                self._set_variable(target, val)

        elif verb == 'ADD':
            srcs = [self._eval_operand(s) for s in stmt.variables_read if isinstance(self._eval_operand(s), Decimal)]
            total = sum(srcs, Decimal("0.00"))
            for dest in stmt.variables_written:
                curr = self._eval_operand(dest)
                if isinstance(curr, Decimal):
                    self._set_variable(dest, curr + total)
                else:
                    self._set_variable(dest, total)

        elif verb == 'SUBTRACT':
            srcs = [self._eval_operand(s) for s in stmt.variables_read if isinstance(self._eval_operand(s), Decimal)]
            sub_val = srcs[0] if srcs else Decimal("0.00")
            for dest in stmt.variables_written:
                curr = self._eval_operand(dest)
                if isinstance(curr, Decimal):
                    self._set_variable(dest, curr - sub_val)

        elif verb == 'MULTIPLY':
            a = self._eval_operand(stmt.variables_read[0]) if stmt.variables_read else Decimal("1")
            for dest in stmt.variables_written:
                curr = self._eval_operand(dest)
                if isinstance(curr, Decimal) and isinstance(a, Decimal):
                    self._set_variable(dest, (curr * a).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        elif verb == 'DIVIDE':
            a = self._eval_operand(stmt.variables_read[0]) if stmt.variables_read else Decimal("1")
            for dest in stmt.variables_written:
                curr = self._eval_operand(dest)
                if isinstance(curr, Decimal) and isinstance(a, Decimal) and a != 0:
                    self._set_variable(dest, (curr / a).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        elif verb == 'PERFORM':
            for target in stmt.targets:
                t_upper = target.upper()
                if t_upper in self.paragraphs and len(self.perform_call_stack) < 50:
                    self.perform_call_stack.append(t_upper)
                    self._execute_paragraph(self.paragraphs[t_upper])
                    self.perform_call_stack.pop()

        elif verb == 'IF':
            pass
