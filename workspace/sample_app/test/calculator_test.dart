import 'package:test/test.dart';
import '../lib/calculator.dart';

void main() {
  test('addition works', () {
    expect(add(2, 3), equals(5));
  });

  test('multiplication works', () {
    expect(multiply(3, 4), equals(12));
  });

  test('square works', () {
    expect(square(5), equals(25));
  });
}
