import unittest
from src.main import check_read_tool

class SafetyTest(unittest.TestCase):
    def test_read_names(self):
        for name in ("get_sprints", "list_items", "fetch_projects"):
            check_read_tool(name)
    def test_write_names_rejected(self):
        for name in ("delete_sprints", "update_items", "create_task", ""):
            with self.assertRaises(ValueError):
                check_read_tool(name)

if __name__ == "__main__":
    unittest.main()
