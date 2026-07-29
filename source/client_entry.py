from integrated_workbench.app import WorkbenchApp
from integrated_workbench.protection import guarded_main


if __name__ == "__main__":
    guarded_main(WorkbenchApp)
