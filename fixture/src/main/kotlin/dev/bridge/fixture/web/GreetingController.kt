package dev.bridge.fixture.web

import dev.bridge.fixture.domain.Greeting
import dev.bridge.fixture.service.GreetingService
import org.springframework.http.ResponseEntity
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.PathVariable
import org.springframework.web.bind.annotation.PostMapping
import org.springframework.web.bind.annotation.RequestBody
import org.springframework.web.bind.annotation.RequestMapping
import org.springframework.web.bind.annotation.RestController

data class GreetingRequest(val recipient: String, val message: String)

@RestController
@RequestMapping("/greetings")
class GreetingController(private val service: GreetingService) {

    @GetMapping
    fun list(): List<Greeting> = service.all()

    @GetMapping("/{recipient}")
    fun forRecipient(@PathVariable recipient: String): ResponseEntity<List<Greeting>> {
        val found = service.forRecipient(recipient)
        return if (found.isEmpty()) ResponseEntity.notFound().build()
        else ResponseEntity.ok(found)
    }

    @PostMapping
    fun create(@RequestBody request: GreetingRequest): Greeting =
        service.record(request.recipient, request.message)
}
