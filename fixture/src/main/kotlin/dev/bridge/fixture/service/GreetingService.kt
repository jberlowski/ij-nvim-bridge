package dev.bridge.fixture.service

import dev.bridge.fixture.domain.Greeting
import dev.bridge.fixture.domain.GreetingRepository
import org.springframework.stereotype.Service
import org.springframework.transaction.annotation.Transactional

@Service
class GreetingService(private val repository: GreetingRepository) {

    @Transactional(readOnly = true)
    fun all(): List<Greeting> = repository.findAll()

    @Transactional(readOnly = true)
    fun forRecipient(recipient: String): List<Greeting> =
        repository.findByRecipientIgnoreCase(recipient)

    @Transactional
    fun record(recipient: String, message: String): Greeting =
        repository.save(Greeting(recipient = recipient, message = message))
}
