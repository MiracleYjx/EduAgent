# Python 基础教程

## 第1章 Python 简介与环境搭建

### 1.1 什么是 Python
Python 是一种高级、解释型、通用的编程语言。它由 Guido van Rossum 于 1989 年圣诞节期间开始设计，并于 1991 年首次发布。Python 的设计哲学强调代码的可读性和简洁性，其语法允许程序员用比 C++ 或 Java 更少的代码行表达概念。

Python 是动态类型语言，支持多种编程范式，包括面向对象、过程式和函数式编程。它拥有庞大的标准库和第三方库生态系统，广泛应用于 Web 开发、数据分析、人工智能、科学计算和自动化脚本等领域。

### 1.2 安装 Python
要开始使用 Python，首先需要在你的计算机上安装 Python 解释器。可以访问 Python 官方网站下载适合你操作系统的安装包。安装时，请确保勾选“Add Python to PATH”选项，以便在命令行中直接使用 Python。

安装完成后，打开终端或命令提示符，输入 `python --version` 或 `python3 --version`，如果显示版本号，说明安装成功。

对于初学者，推荐使用 Anaconda 发行版，它包含了 Python 以及许多常用的科学计算库，并提供了包管理和环境管理工具 conda。

### 1.3 第一个 Python 程序
让我们从经典的“Hello, World!”程序开始。在文本编辑器中输入以下代码，并保存为 `hello.py`：

```python
print("Hello, World!")
```

在命令行中运行 `python hello.py`，你将在屏幕上看到输出：

```
Hello, World!
```

`print()` 是 Python 的内置函数，用于将指定的内容输出到控制台。字符串可以用单引号或双引号括起来。

注意：Python 对大小写敏感，`Print` 和 `print` 是不同的。缩进在 Python 中非常重要，它用于定义代码块，而不是像其他语言那样使用大括号。

## 第2章 变量与数据类型

### 2.1 变量与赋值
变量是用于存储数据的命名位置。在 Python 中，你不需要声明变量的类型，直接赋值即可创建变量。例如：

```python
x = 10
name = "Alice"
is_student = True
```

这里，`x` 是整数，`name` 是字符串，`is_student` 是布尔值。Python 是动态类型语言，变量的类型可以在运行时改变。

变量名必须遵循标识符规则：以字母或下划线开头，后跟字母、数字或下划线；不能是 Python 的关键字。

### 2.2 基本数据类型
Python 有几个内置的基本数据类型：

- 整数（int）：如 10, -5, 0
- 浮点数（float）：如 3.14, -0.5, 2.0
- 字符串（str）：如 "hello", 'Python'
- 布尔值（bool）：True 和 False
- 空值（NoneType）：None

你可以使用 `type()` 函数来查看变量的类型：

```python
print(type(10))        # <class 'int'>
print(type(3.14))      # <class 'float'>
print(type("hello"))   # <class 'str'>
print(type(True))      # <class 'bool'>
print(type(None))      # <class 'NoneType'>
```

注意：Python 的整数没有大小限制，可以表示任意大的整数。浮点数遵循 IEEE 754 双精度标准。

### 2.3 类型转换
有时需要在不同类型之间转换。Python 提供了内置函数进行显式类型转换：

```python
num_str = "123"
num_int = int(num_str)      # 转换为整数
num_float = float(num_str)  # 转换为浮点数
str_num = str(456)          # 转换为字符串
bool_val = bool(0)          # 转换为布尔值，0 为 False，非0为 True
```

转换时要注意可能引发的错误，例如 `int("abc")` 会抛出 `ValueError`。

## 第3章 运算符与表达式

### 3.1 算术运算符
Python 支持常见的算术运算符：加 `+`、减 `-`、乘 `*`、除 `/`、整除 `//`、取模 `%`、幂 `**`。

```python
a = 10
b = 3
print(a + b)   # 13
print(a - b)   # 7
print(a * b)   # 30
print(a / b)   # 3.3333333333333335
print(a // b)  # 3
print(a % b)   # 1
print(a ** b)  # 1000
```

注意：除法 `/` 总是返回浮点数，即使能整除。整除 `//` 返回向下取整的结果。

### 3.2 比较运算符与逻辑运算符
比较运算符用于比较两个值，返回布尔值：`==`、`!=`、`>`、`<`、`>=`、`<=`。

逻辑运算符用于组合布尔表达式：`and`、`or`、`not`。

```python
x = 5
y = 10
print(x < y)        # True
print(x == y)       # False
print(x < y and y > 0)  # True
print(not (x > y))  # True
```

注意：Python 中的逻辑运算符具有短路特性，即 `and` 和 `or` 会根据前面的结果决定是否计算后面的表达式。

### 3.3 赋值运算符与表达式
除了基本的 `=`，Python 还支持复合赋值运算符，如 `+=`、`-=`、`*=`、`/=` 等。

```python
count = 0
count += 1   # 等价于 count = count + 1
count *= 2   # 等价于 count = count * 2
print(count) # 2
```

表达式是由运算符和操作数组成的序列，它会产生一个值。例如 `2 + 3 * 4` 是一个表达式，其值为 14。

## 第4章 控制流

### 4.1 条件语句
条件语句允许程序根据条件执行不同的代码块。Python 使用 `if`、`elif`、`else` 关键字。

```python
score = 85
if score >= 90:
    print("优秀")
elif score >= 60:
    print("及格")
else:
    print("不及格")
```

运行结果：及格

注意：冒号和缩进是必须的。Python 使用缩进来表示代码块，通常用 4 个空格。

### 4.2 循环语句
Python 有两种循环：`for` 循环和 `while` 循环。

`for` 循环用于遍历序列（如列表、字符串、range 对象）：

```python
for i in range(5):
    print(i)
```

输出 0 到 4。

`while` 循环在条件为真时重复执行：

```python
n = 0
while n < 5:
    print(n)
    n += 1
```

输出同上。

注意：要避免无限循环，确保循环条件最终会变为假。可以使用 `break` 提前退出循环，`continue` 跳过当前迭代。

### 4.3 循环中的控制语句
`break` 用于立即终止整个循环；`continue` 用于跳过当前迭代，进入下一次迭代。

```python
for i in range(10):
    if i == 3:
        continue
    if i == 8:
        break
    print(i)
```

输出：0, 1, 2, 4, 5, 6, 7

注意：`break` 和 `continue` 只能用在循环内部。

## 第5章 函数

### 5.1 定义与调用函数
函数是组织好的、可重复使用的代码块。使用 `def` 关键字定义函数。

```python
def greet(name):
    """向指定的人问好"""
    return f"Hello, {name}!"

message = greet("Alice")
print(message)  # Hello, Alice!
```

函数可以接受参数，并可选地返回值。如果没有 `return`，函数返回 `None`。

注意：文档字符串（docstring）是可选的，但推荐添加以说明函数用途。

### 5.2 参数传递
Python 支持多种参数传递方式：位置参数、默认参数、关键字参数、可变参数。

```python
def power(base, exponent=2):
    return base ** exponent

print(power(3))        # 9
print(power(3, 3))     # 27
print(power(exponent=3, base=2))  # 8
```

可变参数：`*args` 接收任意数量的位置参数，`**kwargs` 接收任意数量的关键字参数。

```python
def sum_all(*args):
    return sum(args)

print(sum_all(1, 2, 3))  # 6
```

注意：默认参数的值在函数定义时计算，因此避免使用可变对象作为默认值。

### 5.3 变量作用域
Python 中的作用域分为局部作用域、嵌套作用域、全局作用域和内置作用域。在函数内部赋值的变量是局部的，除非使用 `global` 或 `nonlocal` 声明。

```python
x = 10
def change():
    global x
    x = 20

change()
print(x)  # 20
```

注意：尽量避免使用 `global`，因为它会使代码难以调试。

## 第6章 数据结构

### 6.1 列表
列表是可变的有序序列，用方括号表示。

```python
fruits = ["apple", "banana", "cherry"]
fruits.append("orange")
fruits[1] = "blueberry"
print(fruits)  # ['apple', 'blueberry', 'cherry', 'orange']
```

列表支持索引、切片、添加、删除等操作。

注意：列表是可变对象，修改会影响原列表。

### 6.2 元组与集合
元组是不可变的有序序列，用圆括号表示。

```python
point = (3, 4)
x, y = point
print(x, y)  # 3 4
```

集合是无序的不重复元素集，用花括号或 `set()` 创建。

```python
s = {1, 2, 3, 2}
print(s)  # {1, 2, 3}
```

注意：元组可以作为字典的键，而列表不能。集合用于去重和成员测试。

### 6.3 字典
字典是键值对的集合，用花括号表示。

```python
person = {"name": "Alice", "age": 25}
print(person["name"])  # Alice
person["age"] = 26
print(person)  # {'name': 'Alice', 'age': 26}
```

字典的键必须是不可变类型，如字符串、数字或元组。

注意：从 Python 3.7 开始，字典保持插入顺序。

## 第7章 字符串处理

### 7.1 字符串基础
字符串是字符的序列，可以用单引号、双引号或三引号定义。

```python
s1 = 'hello'
s2 = "world"
s3 = '''多行
字符串'''
print(s1 + " " + s2)  # hello world
```

字符串是不可变的，任何修改操作都会返回新字符串。

注意：三引号常用于多行字符串或文档字符串。

### 7.2 常用字符串方法
Python 提供了丰富的字符串方法，如 `upper()`、`lower()`、`strip()`、`split()`、`join()`、`replace()` 等。

```python
text = "  Hello, World!  "
print(text.strip())          # "Hello, World!"
print(text.upper())          # "  HELLO, WORLD!  "
print(text.split(","))       # ['  Hello', ' World!  ']
print("-".join(["a", "b"])) # "a-b"
```

注意：字符串方法不会改变原字符串，而是返回新字符串。

### 7.3 格式化字符串
Python 支持多种字符串格式化方式：百分号 `%`、`str.format()` 和 f-string（Python 3.6+）。

```python
name = "Alice"
age = 25
print("My name is %s, I am %d." % (name, age))
print("My name is {}, I am {}.".format(name, age))
print(f"My name is {name}, I am {age}.")
```

推荐使用 f-string，它简洁且可读性好。

注意：f-string 在运行时求值，可以包含表达式。

## 第8章 文件操作

### 8.1 打开与关闭文件
使用 `open()` 函数打开文件，它返回文件对象。操作完成后应调用 `close()` 关闭文件，或使用 `with` 语句自动管理。

```python
# 写入文件
with open("example.txt", "w", encoding="utf-8") as f:
    f.write("Hello, file!\n")

# 读取文件
with open("example.txt", "r", encoding="utf-8") as f:
    content = f.read()
    print(content)
```

注意：使用 `with` 可以确保文件正确关闭，即使发生异常。

### 8.2 文件读写模式
`open()` 的第二个参数指定模式：`'r'` 只读，`'w'` 写入（覆盖），`'a'` 追加，`'b'` 二进制模式，`'+'` 读写模式。

```python
with open("data.txt", "a", encoding="utf-8") as f:
    f.write("追加一行\n")
```

注意：写入模式会清空文件原有内容，追加模式则在末尾添加。

### 8.3 读取大文件
对于大文件，应逐行读取以节省内存。

```python
with open("large_file.txt", "r", encoding="utf-8") as f:
    for line in f:
        print(line.strip())
```

注意：`read()` 一次性读取整个文件，`readline()` 读取一行，`readlines()` 读取所有行到列表。

## 第9章 异常处理

### 9.1 异常的概念
异常是程序运行时发生的错误。Python 使用 `try`、`except`、`else`、`finally` 进行异常处理。

```python
try:
    num = int(input("请输入一个数字："))
    result = 10 / num
except ValueError:
    print("输入的不是数字！")
except ZeroDivisionError:
    print("不能除以零！")
else:
    print(f"结果是 {result}")
finally:
    print("执行完毕。")
```

注意：`else` 在无异常时执行，`finally` 总是执行，常用于资源清理。

### 9.2 捕获与抛出异常
可以使用 `raise` 主动抛出异常，并自定义异常信息。

```python
def check_age(age):
    if age < 0:
        raise ValueError("年龄不能为负数")
    print("年龄有效")

try:
    check_age(-1)
except ValueError as e:
    print(e)
```

注意：捕获异常时应指定具体的异常类型，避免使用裸 `except`。

### 9.3 自定义异常
可以通过继承 `Exception` 类来创建自定义异常。

```python
class MyError(Exception):
    pass

try:
    raise MyError("自定义错误")
except MyError as e:
    print(e)
```

注意：自定义异常通常用于特定领域的错误处理。

## 第10章 模块与包

### 10.1 导入模块
模块是包含 Python 代码的文件。使用 `import` 导入模块。

```python
import math
print(math.sqrt(16))  # 4.0

from math import pi
print(pi)  # 3.141592653589793
```

注意：应避免使用 `from module import *`，因为它会导入所有名称，可能引起命名冲突。

### 10.2 创建模块
任何 `.py` 文件都可以作为模块导入。例如，创建 `mymodule.py`：

```python
def greet(name):
    return f"Hello, {name}!"
```

然后在另一个文件中导入：

```python
import mymodule
print(mymodule.greet("Alice"))
```

注意：模块搜索路径包括当前目录、PYTHONPATH 和标准库路径。

### 10.3 包
包是包含多个模块的目录，其中必须包含 `__init__.py` 文件（Python 3.3+ 可选）。例如：

```
my_package/
    __init__.py
    module1.py
    module2.py
```

导入包中的模块：

```python
from my_package import module1
module1.func()
```

注意：包可以嵌套，使用点号访问子包。

通过本教程的学习，你已经掌握了 Python 的基础知识。继续实践和探索，你将能够构建更复杂的应用程序。