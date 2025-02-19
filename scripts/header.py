
import glob

HEADER = """# (C) Copyright IBM Corp. 2019, 2020, 2021, 2022.

#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at

#           http://www.apache.org/licenses/LICENSE-2.0

#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.
"""

HEADER_ = [item + '\n' for item in HEADER.split('\n')]

modules_names = ['simulai', 'examples']

for module_name in modules_names:

    print(f"Entering directory {module_name}")

    py_files = glob.glob(f"{module_name}/**/*.py", recursive=True)

    for pyf in py_files:

        print(f"Updating header for the file {pyf}.")

        with open(pyf, 'r') as fp:
            CONTENT = fp.readlines()
            NEW_CONTENT = HEADER_ + CONTENT

        content = ''.join(CONTENT)

        if HEADER in content:
            print("This file already has the header.")
        else:
            with open(pyf, 'w') as fp:
                fp.writelines(NEW_CONTENT)

