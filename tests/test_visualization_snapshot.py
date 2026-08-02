import unittest

from tools.build_visualization_snapshot import (
    build_snapshot,
    choose_event_date,
    classify_transaction_type,
    normalize_entity,
    split_entities,
)


class VisualizationSnapshotTests(unittest.TestCase):
    def test_transaction_type_precedence(self):
        self.assertEqual(
            classify_transaction_type("samr_simple_case_notices", "甲公司新设合营企业合并案"),
            "新设合营",
        )
        self.assertEqual(
            classify_transaction_type("samr_simple_case_notices", "甲公司收购乙公司股权案"),
            "收购",
        )
        self.assertIsNone(classify_transaction_type("samr_enforcement_cases", "行政处罚"))

    def test_entity_normalization_and_delimiters(self):
        self.assertEqual(normalize_entity("  甲公司（中国）  "), "甲公司(中国)")
        self.assertEqual(split_entities("甲公司，乙公司、甲公司; 丙公司"), ["丙公司", "乙公司", "甲公司"])
        self.assertEqual(split_entities("AIC Parent， Inc.，博裕公司"), ["AIC Parent Inc.", "博裕公司"])
        self.assertEqual(split_entities("ESG控股有限公司（ESG Holdings Limited，“ESG”），正歆有限公司"), ["ESG控股有限公司(ESG Holdings Limited “ESG”)", "正歆有限公司"])

    def test_date_falls_back_to_path_month(self):
        parsed = choose_event_date(
            {
                "dataset": "samr_simple_case_notices",
                "receiveTime": "",
                "list_date": "",
                "pub_date": "",
                "file_path": "samr_simple_case_notices/files/2024/08/case/file.docx",
            }
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["date"], "2024-08")
        self.assertEqual(parsed["source"], "file_path")
        self.assertEqual(parsed["granularity"], "month")

    def test_case_grouping_keeps_dataset_boundaries_and_files(self):
        rows = [
            {
                "dataset": "samr_simple_case_notices",
                "id": "same-id",
                "dedup_key": "simple::body",
                "caseName": "甲公司收购乙公司股权案",
                "caseNo": "20240001",
                "empName": "甲公司，乙公司",
                "receiveTime": "2024-01-02",
                "file_path": "simple/files/2024/01/case/body.docx",
                "file_name": "body.docx",
                "file_ext": ".docx",
                "file_size": "10",
            },
            {
                "dataset": "samr_simple_case_notices",
                "id": "same-id",
                "dedup_key": "simple::attachment",
                "caseName": "甲公司收购乙公司股权案",
                "caseNo": "20240001",
                "empName": "甲公司，乙公司",
                "receiveTime": "2024-01-02",
                "file_path": "simple/files/2024/01/case/attachment.pdf",
                "file_name": "attachment.pdf",
                "file_ext": ".pdf",
                "file_size": "20",
                "record_type": "attachment",
            },
            {
                "dataset": "samr_enforcement_cases",
                "id": "same-id",
                "dedup_key": "enforcement::body",
                "category_label": "行政处罚案件",
                "caseName": "行政处罚决定书",
                "list_date": "2024-02-03",
                "file_path": "enforcement/files/行政处罚案件/2024/02/case/body.md",
                "file_name": "body.md",
                "file_ext": ".md",
                "file_size": "30",
                "record_type": "article_body",
            },
        ]
        snapshot = build_snapshot(rows, generated_at="2026-08-02T00:00:00+08:00")
        self.assertEqual(snapshot["meta"]["fileCount"], 3)
        self.assertEqual(snapshot["meta"]["caseCount"], 2)
        self.assertEqual(
            {item["key"]: item["caseCount"] for item in snapshot["datasets"]},
            {"samr_simple_case_notices": 1, "samr_enforcement_cases": 1},
        )
        simple_case = next(case for case in snapshot["cases"] if case["dataset"] == "samr_simple_case_notices")
        self.assertEqual(simple_case["fileCount"], 2)
        self.assertEqual(simple_case["transactionType"], "收购")
        self.assertEqual(simple_case["entities"], ["乙公司", "甲公司"])
        self.assertFalse(any(source == target for source, target in ((edge["source"], edge["target"]) for edge in snapshot["entityGraph"]["edges"])))


if __name__ == "__main__":
    unittest.main()
